"""Telegram bot extension - multi-session bridge to Claude Code via tmux."""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.extension import Extension
from core.session import Session, SessionStatus
from core.status import format_status, get_auth_info, get_usage
from extensions.telegram.formatting import chunk_html, escape_html, md_to_tg_html

log = logging.getLogger(__name__)

MAX_TG_MESSAGE = 4000  # Telegram limit is 4096; leave margin
EDIT_CHAR_LIMIT = 3800  # freeze live message before hitting MAX_TG_MESSAGE
MIN_EDIT_INTERVAL = 1.5  # seconds between edit_message_text calls
STREAM_FLUSH_DELAY = 2.0  # seconds to wait before flushing text buffer
TOOL_FLUSH_DELAY = 1.5  # seconds to debounce tool call grouping
STREAM_LEVELS = ("all", "mcp", "none")  # tool_use verbosity levels


@dataclass
class _StreamBuffer:
    """Per-session buffer for debouncing stream text events."""

    chat_id: int
    slot: int
    name: str
    user_id: str = ""
    text_parts: list[str] = field(default_factory=list)
    flush_task: asyncio.Task | None = None
    first_flush_done: bool = False
    # C1: edit-in-place streaming
    live_message_id: int | None = None  # current editable message
    live_text: str = ""  # full displayed text in live message
    last_edit_time: float = 0.0  # monotonic time of last edit
    # C2: tool call grouping
    tool_parts: list[str] = field(default_factory=list)
    tool_flush_task: asyncio.Task | None = None
    # C3: cost footer integration (multi mode)
    last_sent_message_id: int | None = None
    last_sent_text: str = ""  # text of last sent message (for edit-to-append)
    # D1: gap-aware typing
    last_message_time: float = 0.0  # monotonic time of last sent/edited message


class ExtensionImpl(Extension):
    name = "telegram"

    def configure(self, engine, config):
        super().configure(engine, config)
        self.token = config["token"]
        self.allowed_users = set(config.get("allowed_users", []))
        self.working_dir = config.get("working_dir") or os.getcwd()
        self.app: Application | None = None
        self._stream_buffers: dict[str, _StreamBuffer] = {}  # session_id -> buffer
        self._awaiting_text_answer: dict[str, str] = {}  # session_id -> request_id
        self._user_stream_levels: dict[str, str] = {}  # user_id -> "all"|"mcp"|"none"
        self._typing_tasks: dict[str, asyncio.Task] = {}  # session_id -> typing loop task
        self._prompt_message_ids: dict[str, int] = {}  # session_id -> user message_id
        self._reply_threading = config.get("reply_threading", True)
        self._show_prefix = config.get("show_prefix", "auto")
        self._stream_mode: str = config.get("stream_mode", "edit")  # "edit" or "multi"

    def _authorized(self, update: Update) -> bool:
        if not self.allowed_users:
            return True
        user = update.effective_user
        return user is not None and (
            user.id in self.allowed_users or user.username in self.allowed_users
        )

    @property
    def sm(self):
        return self.engine.session_manager

    def _resolve_dir(self, path: str) -> str:
        """Resolve a directory path: ~ expansion, relative to working_dir."""
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.working_dir, path)
        return os.path.normpath(path)

    # -- active session persistence -----------------------------------------

    def _active_map_path(self):
        return self.sm.base_dir / "active_sessions.json"

    def _load_active_map(self) -> dict:
        p = self._active_map_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_active_map(self, data: dict) -> None:
        p = self._active_map_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.rename(p)

    def _get_user_active_session(self, user_id: str) -> str | None:
        """Get the persisted active session for a user (if still valid)."""
        data = self._load_active_map()
        sid = data.get(user_id)
        if sid and sid in self.sm.sessions:
            return sid
        return None

    # -- stream level persistence -------------------------------------------

    def _stream_levels_path(self):
        return self.sm.base_dir / "stream_levels.json"

    def _load_stream_levels(self) -> dict[str, str]:
        p = self._stream_levels_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_stream_levels(self) -> None:
        p = self._stream_levels_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._user_stream_levels), encoding="utf-8")
        tmp.rename(p)

    def _get_stream_level(self, user_id: str) -> str:
        return self._user_stream_levels.get(user_id, "all")

    # -- smart session prefix ------------------------------------------------

    def _session_prefix(self, session, user_id: str) -> str:
        """Return session prefix based on show_prefix config.

        Returns '[#slot name] ' (with trailing space) or '' (empty).
        """
        if self._show_prefix == "always":
            return f"[#{session.slot} {session.name}] "
        if self._show_prefix == "never":
            return ""
        # auto: only show when user has multiple sessions
        if len(self.sm.get_sessions_for_user(user_id)) > 1:
            return f"[#{session.slot} {session.name}] "
        return ""

    # -- delivery callback (called by SessionManager) -----------------------

    async def _deliver_result(
        self,
        session_id: str,
        result_text: str,
        metadata: dict,
    ) -> None:
        session = self.sm.sessions.get(session_id)
        if not session:
            return
        chat_id = session.context.get("chat_id")
        if not chat_id:
            return

        # --- Question from Claude (ask_user) ---
        if metadata.get("is_question"):
            request_id = metadata["request_id"]
            options = metadata.get("options") or []
            prefix = self._session_prefix(session, session.user_id)
            question_text = f"{prefix}\u2753 {result_text}"

            if options:
                keyboard = [
                    [InlineKeyboardButton(opt, callback_data=f"q:{request_id}:{i}")]
                    for i, opt in enumerate(options)
                ]
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "Other...",
                            callback_data=f"q:{request_id}:t",
                        )
                    ]
                )
                await self.app.bot.send_message(
                    chat_id,
                    question_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                self._awaiting_text_answer[session_id] = request_id
                await self.app.bot.send_message(
                    chat_id,
                    f"{question_text}\n\n(Reply with your answer)",
                )
            return

        # --- Heartbeat ---
        if metadata.get("is_heartbeat"):
            elapsed_s = metadata.get("elapsed_s", 0)
            mins = elapsed_s // 60
            prefix = self._session_prefix(session, session.user_id)
            text = f"{prefix}<i>Still working... ({mins}m elapsed)</i>"
            await self._send_chunked(chat_id, text, parse_mode="HTML")
            return

        # --- Stream: text event (debounced) ---
        if metadata.get("is_stream") and metadata.get("stream_type") == "text":
            buf = self._stream_buffers.get(session_id)
            if not buf:
                buf = _StreamBuffer(
                    chat_id=chat_id,
                    slot=session.slot,
                    name=session.name,
                    user_id=session.user_id,
                )
                self._stream_buffers[session_id] = buf
            # Flush any pending tool summaries before text arrives
            if buf.tool_parts:
                await self._flush_tool_buffer(session_id)
            buf.text_parts.append(result_text)
            # Periodic flush: start timer once, delivers every STREAM_FLUSH_DELAY
            if not buf.flush_task or buf.flush_task.done():
                buf.flush_task = asyncio.create_task(self._delayed_flush(session_id))
            return

        # --- Stream: tool_use event (grouped, filtered by verbosity) ---
        if metadata.get("is_stream") and metadata.get("stream_type") == "tool_use":
            level = self._get_stream_level(session.user_id)
            tool_name = metadata.get("tool_name") or ""
            show = level == "all" or (level == "mcp" and tool_name.startswith("mcp__"))
            if show:
                buf = self._stream_buffers.get(session_id)
                if not buf:
                    buf = _StreamBuffer(
                        chat_id=chat_id,
                        slot=session.slot,
                        name=session.name,
                        user_id=session.user_id,
                    )
                    self._stream_buffers[session_id] = buf
                summary = self._format_tool_use(metadata)
                buf.tool_parts.append(summary)
                # Start/restart debounce timer for tool flush
                if buf.tool_flush_task and not buf.tool_flush_task.done():
                    buf.tool_flush_task.cancel()
                buf.tool_flush_task = asyncio.create_task(self._delayed_tool_flush(session_id))
            return

        # --- Stopped ---
        if metadata.get("is_stopped"):
            self._cancel_typing(session_id)
            buf = self._stream_buffers.get(session_id)
            if buf and buf.tool_parts:
                await self._flush_tool_buffer(session_id)
            if self._stream_mode == "edit":
                await self._stream_edit_flush(session_id)
            else:
                await self._flush_stream_buffer(session_id)
            prefix = self._session_prefix(session, session.user_id)
            await self._send_chunked(chat_id, f"{prefix}<b>Stopped</b>", parse_mode="HTML")
            return

        # --- Final result (streaming mode: text already delivered) ---
        if metadata.get("is_final"):
            self._cancel_typing(session_id)
            buf = self._stream_buffers.get(session_id)

            # Flush any pending tool summaries first
            if buf and buf.tool_parts:
                await self._flush_tool_buffer(session_id)

            # Flush remaining text
            if self._stream_mode == "edit":
                flushed = await self._stream_edit_flush(session_id)
            else:
                flushed = await self._flush_stream_buffer(session_id)

            prefix = self._session_prefix(session, session.user_id)
            reply_to = self._prompt_message_ids.get(session_id) if self._reply_threading else None

            # Fallback: if stream buffer was empty (text events were lost
            # during debounce, or never delivered), use result_text from
            # _parse_stream_result as a safety net.
            if not flushed and result_text and not metadata.get("is_error"):
                log.warning(
                    "Stream text was not buffered for session %s, using fallback",
                    session_id[:8],
                )
                msg_id = await self._send_html(
                    chat_id, f"{prefix}{result_text}", reply_to_message_id=reply_to
                )
                if buf and msg_id is not None:
                    buf.last_sent_message_id = msg_id
                    buf.last_sent_text = f"{prefix}{result_text}"

            cost = metadata.get("total_cost_usd")
            turns = metadata.get("num_turns")
            if metadata.get("is_error"):
                err_text = escape_html(result_text or "[Error]")
                await self._send_chunked(
                    chat_id,
                    f"{prefix}<b>\u26a0 Error</b>\n<pre>{err_text}</pre>",
                    parse_mode="HTML",
                )
            elif cost is not None:
                await self._send_cost_footer(session_id, chat_id, prefix, cost, turns)

            self._stream_buffers.pop(session_id, None)
            self._prompt_message_ids.pop(session_id, None)
            return

        # --- Fallback (recovery with full text, backward compat) ---
        if result_text:
            prefix = self._session_prefix(session, session.user_id)
            cost = metadata.get("total_cost_usd")
            turns = metadata.get("num_turns")
            body = f"{prefix}{result_text}"
            footer = ""
            if cost is not None and not metadata.get("is_error"):
                footer = f"\n\n<i>--- ${cost:.4f} | {turns} turns ---</i>"
            await self._send_html(chat_id, f"{body}{footer}")

    # -- stream buffer helpers -----------------------------------------------

    async def _delayed_flush(self, session_id: str) -> None:
        """Wait then flush the text buffer for a session."""
        await asyncio.sleep(STREAM_FLUSH_DELAY)
        if self._stream_mode == "edit":
            await self._stream_edit_flush(session_id)
        else:
            await self._flush_stream_buffer(session_id)

    async def _flush_stream_buffer(self, session_id: str) -> bool:
        """Send accumulated text in the stream buffer, if any (multi mode).

        Returns True if text was flushed (regardless of send success).
        """
        buf = self._stream_buffers.get(session_id)
        if not buf or not buf.text_parts:
            return False
        # Cancel pending flush timer (but not if we ARE the flush task)
        if (
            buf.flush_task
            and not buf.flush_task.done()
            and buf.flush_task is not asyncio.current_task()
        ):
            buf.flush_task.cancel()
        text = "".join(buf.text_parts)
        buf.text_parts.clear()
        buf.flush_task = None
        if text.strip():
            prefix = self._session_prefix_from_buf(buf)
            full_text = f"{prefix}{text}"
            # Reply threading: only on first flush for this prompt
            reply_to = None
            if self._reply_threading and not buf.first_flush_done:
                reply_to = self._prompt_message_ids.get(session_id)
            buf.first_flush_done = True
            msg_id = await self._send_html(buf.chat_id, full_text, reply_to_message_id=reply_to)
            if msg_id is not None:
                buf.last_sent_message_id = msg_id
                buf.last_sent_text = full_text
            return True
        return False

    def _session_prefix_from_buf(self, buf: _StreamBuffer) -> str:
        """Build session prefix from a stream buffer (no Session object needed)."""
        if self._show_prefix == "always":
            return f"[#{buf.slot} {buf.name}] "
        if self._show_prefix == "never":
            return ""
        if len(self.sm.get_sessions_for_user(buf.user_id)) > 1:
            return f"[#{buf.slot} {buf.name}] "
        return ""

    async def _stream_edit_flush(self, session_id: str) -> bool:
        """Flush text buffer using edit-in-place mode.

        Sends a new message or edits the existing live message. When the live
        message approaches the Telegram character limit, it is frozen and a new
        continuation message is started.

        Returns True if text was flushed.
        """
        buf = self._stream_buffers.get(session_id)
        if not buf or not buf.text_parts:
            return False

        # Cancel pending flush timer (but not if we ARE the flush task)
        if (
            buf.flush_task
            and not buf.flush_task.done()
            and buf.flush_task is not asyncio.current_task()
        ):
            buf.flush_task.cancel()

        new_text = "".join(buf.text_parts)
        buf.text_parts.clear()
        buf.flush_task = None

        if not new_text.strip() and not buf.live_text:
            return False

        buf.live_text += new_text
        prefix = self._session_prefix_from_buf(buf)
        display_text = f"{prefix}{buf.live_text}"

        # No live message yet — send a new one
        if buf.live_message_id is None:
            # Reply threading: only on first flush for this prompt
            reply_to = None
            if self._reply_threading and not buf.first_flush_done:
                reply_to = self._prompt_message_ids.get(session_id)
            buf.first_flush_done = True
            msg_id = await self._send_html(
                buf.chat_id,
                display_text[:MAX_TG_MESSAGE],
                reply_to_message_id=reply_to,
            )
            if msg_id is not None:
                buf.live_message_id = msg_id
                buf.last_sent_message_id = msg_id
                buf.last_edit_time = time.monotonic()
            return True

        # Live message exists — check if we need to freeze and start new
        if len(display_text) >= EDIT_CHAR_LIMIT:
            # Freeze current live message (leave it as-is), start new
            buf.live_message_id = None
            buf.live_text = new_text  # carry over only the new chunk
            display_text = f"{prefix}{buf.live_text}"
            msg_id = await self._send_html(buf.chat_id, display_text[:MAX_TG_MESSAGE])
            if msg_id is not None:
                buf.live_message_id = msg_id
                buf.last_sent_message_id = msg_id
                buf.last_edit_time = time.monotonic()
            return True

        # Rate limit: minimum interval between edits
        now = time.monotonic()
        if now - buf.last_edit_time < MIN_EDIT_INTERVAL:
            # Schedule a retry after the remaining cooldown
            if not buf.flush_task or buf.flush_task.done():
                remaining = MIN_EDIT_INTERVAL - (now - buf.last_edit_time)
                buf.flush_task = asyncio.create_task(
                    self._delayed_edit_retry(session_id, remaining)
                )
            return True

        # Edit the live message with HTML formatting
        try:
            html = md_to_tg_html(display_text)
            await self.app.bot.edit_message_text(
                text=html,
                chat_id=buf.chat_id,
                message_id=buf.live_message_id,
                parse_mode="HTML",
            )
            buf.last_edit_time = now
            buf.last_message_time = now
        except BadRequest as e:
            err_msg = str(e).lower()
            if "message is not modified" in err_msg:
                pass  # Text unchanged, ignore
            elif "message to edit not found" in err_msg:
                # Message was deleted, fall back to new message
                log.warning("Live message deleted, sending new message")
                buf.live_message_id = None
                msg_id = await self._send_html(buf.chat_id, display_text[:MAX_TG_MESSAGE])
                if msg_id is not None:
                    buf.live_message_id = msg_id
                    buf.last_sent_message_id = msg_id
                    buf.last_edit_time = time.monotonic()
            else:
                log.warning("edit_message_text failed: %s", e)
                # Try plain text fallback
                try:
                    await self.app.bot.edit_message_text(
                        text=display_text,
                        chat_id=buf.chat_id,
                        message_id=buf.live_message_id,
                    )
                    buf.last_edit_time = now
                    buf.last_message_time = now
                except Exception:
                    log.exception("edit_message_text plain fallback failed")
        except Exception:
            log.exception("edit_message_text failed unexpectedly")

        return True

    async def _delayed_edit_retry(self, session_id: str, delay: float) -> None:
        """Retry an edit flush after rate-limit delay."""
        await asyncio.sleep(delay)
        await self._stream_edit_flush(session_id)

    # -- tool call grouping helpers ------------------------------------------

    async def _flush_tool_buffer(self, session_id: str) -> None:
        """Send accumulated tool call summaries as one message."""
        buf = self._stream_buffers.get(session_id)
        if not buf or not buf.tool_parts:
            return

        # Cancel pending tool flush timer (but not if we ARE the flush task)
        if (
            buf.tool_flush_task
            and not buf.tool_flush_task.done()
            and buf.tool_flush_task is not asyncio.current_task()
        ):
            buf.tool_flush_task.cancel()
        buf.tool_flush_task = None

        tool_text = "\n".join(buf.tool_parts)
        buf.tool_parts.clear()

        if not tool_text.strip():
            return

        # In edit mode: fold into the live message if possible
        if self._stream_mode == "edit" and buf.live_message_id is not None:
            separator = "\n\n"
            prefix = self._session_prefix_from_buf(buf)
            combined = f"{prefix}{buf.live_text}{separator}{tool_text}"
            if len(combined) < EDIT_CHAR_LIMIT:
                buf.live_text += f"{separator}{tool_text}"
                display_text = f"{prefix}{buf.live_text}"
                try:
                    html = md_to_tg_html(display_text)
                    await self.app.bot.edit_message_text(
                        text=html,
                        chat_id=buf.chat_id,
                        message_id=buf.live_message_id,
                        parse_mode="HTML",
                    )
                    buf.last_edit_time = time.monotonic()
                    return
                except BadRequest as e:
                    if "message is not modified" in str(e).lower():
                        return
                    # Fall through to send separately
                    buf.live_text = buf.live_text[: -(len(separator) + len(tool_text))]
                except Exception:
                    buf.live_text = buf.live_text[: -(len(separator) + len(tool_text))]

        # Send as separate message (multi mode, or edit mode overflow)
        prefix = self._session_prefix_from_buf(buf)
        msg_id = await self._send_chunked(buf.chat_id, f"{prefix}{tool_text}")
        if msg_id is not None:
            buf.last_sent_message_id = msg_id

    async def _delayed_tool_flush(self, session_id: str) -> None:
        """Wait then flush the tool buffer for a session."""
        await asyncio.sleep(TOOL_FLUSH_DELAY)
        await self._flush_tool_buffer(session_id)

    # -- cost footer integration ---------------------------------------------

    async def _send_cost_footer(
        self,
        session_id: str,
        chat_id: int,
        prefix: str,
        cost: float,
        turns: int | None,
    ) -> None:
        """Append cost footer to the last message, or send as separate message.

        In edit mode: final edit of the live message with cost footer appended.
        In multi mode: edit the last sent message to append footer.
        Falls back to sending a separate message if edit fails.
        """
        footer = f"\n\n<i>--- ${cost:.4f} | {turns} turns ---</i>"
        buf = self._stream_buffers.get(session_id)

        # Edit mode: append footer to live message via final edit
        if self._stream_mode == "edit" and buf and buf.live_message_id:
            raw_display = f"{prefix}{buf.live_text}"
            html = md_to_tg_html(raw_display) + footer
            if len(html) < MAX_TG_MESSAGE:
                try:
                    await self.app.bot.edit_message_text(
                        text=html,
                        chat_id=chat_id,
                        message_id=buf.live_message_id,
                        parse_mode="HTML",
                    )
                    buf.live_message_id = None
                    return
                except BadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        log.warning("Cost footer edit failed: %s", e)
                except Exception:
                    log.exception("Cost footer edit failed unexpectedly")

        # Multi mode: edit the last sent message to append footer
        if buf and buf.last_sent_message_id and buf.last_sent_text:
            html = md_to_tg_html(buf.last_sent_text) + footer
            if len(html) < MAX_TG_MESSAGE:
                try:
                    await self.app.bot.edit_message_text(
                        text=html,
                        chat_id=chat_id,
                        message_id=buf.last_sent_message_id,
                        parse_mode="HTML",
                    )
                    return
                except BadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        log.warning("Cost footer edit (multi) failed: %s", e)
                except Exception:
                    log.exception("Cost footer edit (multi) failed unexpectedly")

        # Fallback: send as separate message
        await self._send_chunked(
            chat_id,
            f"{prefix}<i>--- ${cost:.4f} | {turns} turns ---</i>",
            parse_mode="HTML",
        )

    @staticmethod
    def _format_tool_use(metadata: dict) -> str:
        """Format a tool_use event into a concise summary."""
        tool_name = metadata.get("tool_name", "Tool")
        tool_input = metadata.get("tool_input", {})

        # Try common field names for a detail snippet
        detail = ""
        for key in ("file_path", "command", "pattern", "description", "prompt", "query", "url"):
            val = tool_input.get(key)
            if val and isinstance(val, str):
                detail = val
                break

        if not detail:
            # For Glob, try "pattern" key
            detail = tool_input.get("glob", "")

        if detail:
            # Truncate long details
            if len(detail) > 60:
                detail = detail[:57] + "..."
            return f"\U0001f527 {tool_name}: {detail}"
        return f"\U0001f527 {tool_name}"

    async def _send_chunked(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Send text in chunks, splitting at newline boundaries when possible.

        Returns the message_id of the last successfully sent message, or None.
        """
        last_message_id: int | None = None
        first_chunk = True
        while text:
            if len(text) <= MAX_TG_MESSAGE:
                chunk = text
                text = ""
            else:
                # Try to split at a newline near the limit
                cut = text.rfind("\n", 0, MAX_TG_MESSAGE)
                if cut < MAX_TG_MESSAGE // 2:
                    cut = MAX_TG_MESSAGE  # No good newline, hard cut
                chunk = text[:cut]
                text = text[cut:].lstrip("\n")
            try:
                kwargs: dict = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
                if first_chunk and reply_to_message_id is not None:
                    kwargs["reply_parameters"] = ReplyParameters(
                        message_id=reply_to_message_id,
                        allow_sending_without_reply=True,
                    )
                msg = await self.app.bot.send_message(**kwargs)
                last_message_id = msg.message_id
                # Update last_message_time for gap-aware typing
                for buf in self._stream_buffers.values():
                    if buf.chat_id == chat_id:
                        buf.last_message_time = time.monotonic()
            except Exception:
                log.exception("Failed to deliver message to chat %s", chat_id)
                break  # subsequent chunks will almost certainly fail too
            first_chunk = False
        return last_message_id

    async def _send_html(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> int | None:
        """Convert markdown to Telegram HTML and send, with plain-text fallback.

        Returns the message_id of the last successfully sent message, or None.
        """
        html = md_to_tg_html(text)
        chunks = chunk_html(html)
        last_message_id: int | None = None
        first_chunk = True
        for chunk in chunks:
            try:
                kwargs: dict = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
                if first_chunk and reply_to_message_id is not None:
                    kwargs["reply_parameters"] = ReplyParameters(
                        message_id=reply_to_message_id,
                        allow_sending_without_reply=True,
                    )
                msg = await self.app.bot.send_message(**kwargs)
                last_message_id = msg.message_id
                # Update last_message_time for gap-aware typing
                for buf in self._stream_buffers.values():
                    if buf.chat_id == chat_id:
                        buf.last_message_time = time.monotonic()
            except BadRequest:
                log.warning("HTML parse failed for chunk, retrying as plain text")
                try:
                    msg = await self.app.bot.send_message(chat_id=chat_id, text=chunk)
                    last_message_id = msg.message_id
                    for buf in self._stream_buffers.values():
                        if buf.chat_id == chat_id:
                            buf.last_message_time = time.monotonic()
                except Exception:
                    log.exception("Plain text fallback failed for chat %s", chat_id)
                    break
            except Exception:
                log.exception("Failed to deliver HTML message to chat %s", chat_id)
                break
            first_chunk = False
        return last_message_id

    # -- typing indicator ----------------------------------------------------

    async def _keep_typing(self, chat_id: int, session_id: str) -> None:
        """Send 'typing' chat action periodically during processing gaps.

        Runs continuously until cancelled. Only sends typing when there's been
        a gap (>3s) since the last message was sent to the chat.
        """
        try:
            while True:
                buf = self._stream_buffers.get(session_id)
                if buf and buf.last_message_time > 0:
                    # Buffer exists with activity — only type during gaps
                    if time.monotonic() - buf.last_message_time > 3.0:
                        try:
                            await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")
                        except Exception:
                            pass
                else:
                    # No buffer yet (pre-first-delivery) — always send typing
                    try:
                        await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")
                    except Exception:
                        pass
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    def _start_typing(self, chat_id: int, session_id: str) -> None:
        """Start the typing indicator loop for a session."""
        self._cancel_typing(session_id)
        self._typing_tasks[session_id] = asyncio.create_task(self._keep_typing(chat_id, session_id))

    def _cancel_typing(self, session_id: str) -> None:
        """Cancel the typing indicator loop for a session, if running."""
        task = self._typing_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    # -- ask_user callback handling ------------------------------------------

    async def _handle_callback_query(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle InlineKeyboard button presses for ask_user questions."""
        query = update.callback_query
        if not query or not query.data or not query.data.startswith("q:"):
            return

        await query.answer()

        # Parse callback_data: "q:{request_id}:{index_or_t}"
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            return
        _, request_id, action = parts

        pending = self.engine.pending
        entry = pending.get(request_id)

        if not entry:
            # Question expired or already answered
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Question expired or already answered.")
            return

        if action == "t":
            # Switch to free-text mode
            session_id = entry.session_id
            self._awaiting_text_answer[session_id] = request_id
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Type your answer:")
            return

        # Numeric index — resolve with the chosen option
        try:
            idx = int(action)
            options = entry.data.get("options", [])
            if 0 <= idx < len(options):
                answer = options[idx]
            else:
                answer = f"Option {idx}"
        except ValueError:
            answer = action

        if pending.resolve(request_id, answer):
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"Answer sent: {answer}")
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Question expired or already answered.")

    # -- helpers ------------------------------------------------------------

    def _resolve_session(self, user_id: str, target: str):
        """Resolve a target string (slot number or name) to a Session.
        Priority: slot number > exact name > name prefix."""
        # Slot number
        if target.isdigit():
            slot = int(target)
            found = self.sm.get_session_by_slot(user_id, slot)
            if found:
                return found

        # Exact name match
        for s in self.sm.sessions.values():
            if s.user_id == user_id and s.name == target:
                return s

        # Prefix match (fallback)
        for s in self.sm.sessions.values():
            if s.user_id == user_id and s.name.startswith(target):
                return s

        return None

    def _set_active(
        self, ctx: ContextTypes.DEFAULT_TYPE, user_id: str, session_id: str | None
    ) -> None:
        """Update active session in both memory and persistent storage."""
        ctx.user_data["active_session_id"] = session_id
        data = self._load_active_map()
        if session_id:
            data[user_id] = session_id
        else:
            data.pop(user_id, None)
        self._save_active_map(data)

    async def _ensure_active_session(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
    ) -> tuple[Session, bool]:
        """Return (session, was_auto_selected).  Creates one if needed."""
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        active_id = ctx.user_data.get("active_session_id")

        # Check if the stored active session still exists
        if active_id and active_id not in self.sm.sessions:
            active_id = None

        # Try to restore from persisted state (survives restart)
        if not active_id:
            persisted = self._get_user_active_session(user_id)
            if persisted:
                ctx.user_data["active_session_id"] = persisted
                active_id = persisted

        if active_id:
            return self.sm.sessions[active_id], False

        # Auto-select: pick first non-dead session, or create new
        user_sessions = self.sm.get_sessions_for_user(user_id)
        alive = [s for s in user_sessions if s.status != SessionStatus.DEAD]
        alive.sort(key=lambda s: (s.status != SessionStatus.IDLE, s.slot))
        if alive:
            session = alive[0]
        else:
            # All dead or none — create a fresh one, name matches slot
            used_slots = {s.slot for s in user_sessions}
            slot_name = "session-1"
            for i in range(1, self.sm.max_sessions_per_user + 1):
                if i not in used_slots:
                    slot_name = f"session-{i}"
                    break
            session = await self.sm.create_session(
                name=slot_name,
                user_id=user_id,
                working_dir=self.working_dir,
                context={"chat_id": chat_id},
            )

        self._set_active(ctx, user_id, session.id)
        return session, True

    # -- command handlers ---------------------------------------------------

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        max_s = self.sm.max_sessions_per_user
        await update.message.reply_text(
            "Claude Code bridge ready.\n\n"
            "Commands:\n"
            f"/new [name] [dir] - Create new session (max {max_s})\n"
            "/sessions - List all sessions\n"
            "/switch <slot|name> - Switch session\n"
            "/status - Auth, usage & session info\n"
            "/stop - Stop running task + clear queue\n"
            "/delete <slot|name> - Delete session\n"
            "/verbose [all|mcp|none] - Tool call verbosity\n\n"
            "Send any message to start working."
        )

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id

        # Parse: /new [name] [working_dir]  or  /new <dir>
        parts = update.message.text.split(maxsplit=2)
        name = None
        work_dir = self.working_dir

        if len(parts) == 2 and os.path.isdir(self._resolve_dir(parts[1].strip())):
            # Single arg is an existing directory — treat as dir, auto-name
            work_dir = self._resolve_dir(parts[1].strip())
        elif len(parts) >= 2:
            name = parts[1].strip()
            if len(parts) >= 3:
                candidate = self._resolve_dir(parts[2].strip())
                if os.path.isdir(candidate):
                    work_dir = candidate
                else:
                    await update.message.reply_text(f"Directory not found: {candidate}")
                    return

        # Auto-name: session-{slot} where slot is the one that will be assigned
        if not name:
            user_sessions = self.sm.get_sessions_for_user(user_id)
            used_slots = {s.slot for s in user_sessions}
            for i in range(1, self.sm.max_sessions_per_user + 1):
                if i not in used_slots:
                    name = f"session-{i}"
                    break
            if not name:
                name = "session"

        # Check name uniqueness
        if self.sm.get_session_by_name(user_id, name):
            await update.message.reply_text(f"Name '{name}' already in use.")
            return

        try:
            session = await self.sm.create_session(
                name=name,
                user_id=user_id,
                working_dir=work_dir,
                context={"chat_id": chat_id},
            )
        except RuntimeError as e:
            await update.message.reply_text(str(e))
            return

        self._set_active(ctx, user_id, session.id)
        dir_note = f"\nDir: {work_dir}" if work_dir != self.working_dir else ""
        await update.message.reply_text(
            f"Created #{session.slot} '{session.name}'. Now active.{dir_note}"
        )

    async def _cmd_sessions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        user_id = str(update.effective_user.id)
        active_id = ctx.user_data.get("active_session_id")

        sessions = self.sm.get_sessions_for_user(user_id)
        if not sessions:
            await update.message.reply_text("No sessions. Send a message to create one.")
            return

        lines = ["Sessions (* = active):"]
        for s in sessions:
            marker = " *" if s.id == active_id else ""
            status_tag = s.status.value.upper()
            # Show working dir if it differs from the default
            dir_info = ""
            if s.working_dir != self.working_dir:
                dir_name = os.path.basename(s.working_dir.rstrip("/"))
                dir_info = f" ({dir_name})"
            prompt_info = ""
            if s.last_prompt:
                prompt_info = f"\n      {s.last_prompt[:50]}"
            lines.append(f"  #{s.slot}. {s.name} [{status_tag}]{marker}{dir_info}{prompt_info}")

        await update.message.reply_text("\n".join(lines))

    async def _cmd_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("Usage: /switch <slot number or name>")
            return

        target = args[1].strip()
        user_id = str(update.effective_user.id)
        found = self._resolve_session(user_id, target)

        if not found:
            await update.message.reply_text(f"Session '{target}' not found.")
            return

        self._set_active(ctx, user_id, found.id)
        await update.message.reply_text(
            f"Switched to #{found.slot} '{found.name}' [{found.status.value}]."
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.chat.send_action("typing")

        try:
            active_id = ctx.user_data.get("active_session_id")
            session_meta = None
            if active_id and active_id in self.sm.sessions:
                session_meta = self.sm.sessions[active_id].last_result_metadata or None

            auth, usage = await get_auth_info(), await get_usage()
            text = format_status(auth, usage, session_meta)

            # Append extension health report
            ext_report = await self._build_health_report()
            if ext_report:
                text += "\n" + ext_report

            await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")
        except Exception:
            log.exception("/status failed")
            await update.message.reply_text("Failed to fetch status. Check logs.")

    async def _build_health_report(self) -> str:
        """Build extension health + tools summary via async health checks."""
        registry = self.engine.registry
        if not registry:
            return ""

        mcp_tools = self.sm.list_mcp_tools() if self.sm else {}
        health = await registry.health_check_all()

        lines = ["-- Extensions --"]
        for ext in registry.extensions:
            name = ext.name
            hc = health.get(name, {})
            status = hc.get("status", "ok")
            icon = {"ok": "+", "degraded": "~", "error": "!"}.get(status, "?")

            # Collect info parts
            parts: list[str] = []
            tool_count = len(mcp_tools.get(name, []))
            if tool_count:
                label = "tool" if tool_count == 1 else "tools"
                parts.append(f"{tool_count} {label}")

            # Extension-specific details from health_check
            for key in ("secrets", "files", "jobs"):
                if key in hc:
                    parts.append(f"{hc[key]} {key}")
            if hc.get("scheduler"):
                parts.append(hc["scheduler"])
            if hc.get("polling") is True:
                parts.append("polling active")

            # Heartbeat-specific details
            if "runs_today" in hc:
                parts.append(f"{hc['runs_today']} runs today")
            if "consecutive_noop" in hc:
                parts.append(f"{hc['consecutive_noop']} idle")
            if hc.get("next_run"):
                from core.status import relative_time

                next_str = relative_time(hc["next_run"])
                if next_str:
                    parts.append(f"next {next_str}")
            eff = hc.get("effective_interval", hc.get("interval"))
            if eff:
                parts.append(f"{eff}s interval")
                mult = hc.get("backoff_multiplier", 1)
                if mult > 1:
                    parts.append(f"{mult}x backoff")

            line = f"  [{icon}] {name}"
            if parts:
                line += f"  {', '.join(parts)}"
            lines.append(line)

            # Policies sub-line
            policies = hc.get("policies")
            if policies:
                policy_parts = []
                for pk, pv in policies.items():
                    policy_parts.append(f"{pk} ({pv})" if isinstance(pv, int) else f"{pk}: {pv}")
                lines.append(f"      policies: {', '.join(policy_parts)}")

        return "\n".join(lines)

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        active_id = ctx.user_data.get("active_session_id")
        if not active_id or active_id not in self.sm.sessions:
            await update.message.reply_text("No active session.")
            return

        session = self.sm.sessions[active_id]

        # Allow stop for BUSY (kills task + drains) and IDLE (drains queue only)
        if session.status not in (SessionStatus.BUSY, SessionStatus.IDLE):
            await update.message.reply_text(
                f"#{session.slot} '{session.name}' is {session.status.value}."
            )
            return

        stopped, drained = await self.sm.stop_session(active_id)

        # Cancel typing indicator and any pending ask_user questions
        self._cancel_typing(active_id)
        self.engine.pending.cancel_for_session(active_id)
        self._awaiting_text_answer.pop(active_id, None)

        if stopped:
            msg = f"Stopping #{session.slot} '{session.name}'..."
            if drained:
                msg += f" ({drained} queued cleared)"
            await update.message.reply_text(msg)
        elif drained:
            await update.message.reply_text(
                f"Cleared {drained} queued message(s) from #{session.slot} '{session.name}'."
            )
        else:
            await update.message.reply_text(
                f"#{session.slot} '{session.name}' has nothing to stop."
            )

    async def _cmd_delete(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        user_id = str(update.effective_user.id)

        # Parse: /delete <target> [force]
        parts = update.message.text.split()
        if len(parts) < 2:
            await update.message.reply_text("Usage: /delete <slot|name> [force]")
            return

        force = len(parts) >= 3 and parts[-1].lower() == "force"
        target = parts[1].strip()

        found = self._resolve_session(user_id, target)
        if not found:
            await update.message.reply_text(f"Session '{target}' not found.")
            return

        # Guard: don't delete busy sessions without force
        if found.status == SessionStatus.BUSY and not force:
            await update.message.reply_text(
                f"#{found.slot} '{found.name}' is busy. "
                f"Use /stop first, or /delete {found.slot} force"
            )
            return

        slot = found.slot
        name = found.name
        session_id = found.id

        # Clean up typing, stream buffer, prompt ids, and stale ask_user state
        self._cancel_typing(session_id)
        buf = self._stream_buffers.pop(session_id, None)
        if buf:
            if buf.flush_task and not buf.flush_task.done():
                buf.flush_task.cancel()
            if buf.tool_flush_task and not buf.tool_flush_task.done():
                buf.tool_flush_task.cancel()
        self._awaiting_text_answer.pop(session_id, None)
        self._prompt_message_ids.pop(session_id, None)

        await self.sm.destroy_session(session_id)

        if ctx.user_data.get("active_session_id") == session_id:
            self._set_active(ctx, user_id, None)

        # Clean up any other active_sessions references to this session
        data = self._load_active_map()
        dirty = False
        for uid, sid in list(data.items()):
            if sid == session_id:
                data.pop(uid)
                dirty = True
        if dirty:
            self._save_active_map(data)

        await update.message.reply_text(f"Deleted #{slot} '{name}'.")

    async def _cmd_verbose(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        user_id = str(update.effective_user.id)
        parts = update.message.text.split(maxsplit=1)

        if len(parts) >= 2:
            level = parts[1].strip().lower()
            if level not in STREAM_LEVELS:
                await update.message.reply_text(f"Unknown level '{level}'. Use: all, mcp, none")
                return
        else:
            # Cycle to next level
            current = self._get_stream_level(user_id)
            idx = STREAM_LEVELS.index(current) if current in STREAM_LEVELS else 0
            level = STREAM_LEVELS[(idx + 1) % len(STREAM_LEVELS)]

        self._user_stream_levels[user_id] = level
        self._save_stream_levels()

        labels = {
            "all": "all tool calls",
            "mcp": "MCP tool calls only",
            "none": "no tool calls",
        }
        await update.message.reply_text(f"Stream verbosity: {level} ({labels[level]})")

    # -- message handler ----------------------------------------------------

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = update.message.text
        if not text:
            return

        # --- Intercept text answer for pending ask_user question ---
        active_id = ctx.user_data.get("active_session_id")
        if active_id and active_id in self._awaiting_text_answer:
            request_id = self._awaiting_text_answer.pop(active_id)
            if self.engine.pending.resolve(request_id, text):
                await update.message.reply_text(f"Answer sent: {text[:100]}")
                return
            # Question expired before user replied — notify and discard
            await update.message.reply_text(
                "Question timed out. The session continued without your answer."
            )
            return

        session, auto_selected = await self._ensure_active_session(update, ctx)

        # Dead sessions need manual intervention
        if session.status == SessionStatus.DEAD:
            await update.message.reply_text(
                f"#{session.slot} '{session.name}' is dead. "
                f"Use /delete {session.slot} and /new to start fresh."
            )
            return

        # Update chat_id in context in case it changed
        session.context["chat_id"] = update.effective_chat.id

        try:
            position = await self.sm.send_prompt(session.id, text)
        except RuntimeError as e:
            await update.message.reply_text(str(e))
            return

        prefix = self._session_prefix(session, str(update.effective_user.id))
        auto_note = "(auto-activated) " if auto_selected else ""

        if position == 0:
            await update.message.reply_text(
                f"{prefix}{auto_note}<i>Processing...</i>", parse_mode="HTML"
            )
            self._start_typing(update.effective_chat.id, session.id)
        else:
            await update.message.reply_text(
                f"{prefix}{auto_note}Queued (position {position}). "
                f"Use /new to start a parallel session."
            )

        self._prompt_message_ids[session.id] = update.message.message_id

    # -- media handler ------------------------------------------------------

    async def _handle_media(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle photo and document messages by downloading and prompting Claude."""
        if not self._authorized(update):
            return

        session, auto_selected = await self._ensure_active_session(update, ctx)

        if session.status == SessionStatus.DEAD:
            await update.message.reply_text(
                f"#{session.slot} '{session.name}' is dead. "
                f"Use /delete {session.slot} and /new to start fresh."
            )
            return

        session.context["chat_id"] = update.effective_chat.id

        msg = update.message

        # Determine file_id and build filename
        if msg.photo:
            # photo is a list of PhotoSize; take the largest
            photo = msg.photo[-1]
            file_id = photo.file_id
            file_unique_id = photo.file_unique_id
            original_name = "photo.jpg"
            media_type = "image"
        elif msg.document:
            file_id = msg.document.file_id
            file_unique_id = msg.document.file_unique_id
            original_name = msg.document.file_name or "file"
            media_type = "file"
        else:
            return

        # Safe filename: {stem}_{unique_id}{ext}
        p = Path(original_name)
        safe_name = f"{p.stem}_{file_unique_id}{p.suffix}"

        # Download to uploads dir inside session working_dir
        uploads_dir = Path(session.working_dir) / ".claude-ext-uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / safe_name

        try:
            tg_file = await self.app.bot.get_file(file_id)
            await tg_file.download_to_drive(str(dest))
        except Exception:
            log.exception("Failed to download media file")
            await update.message.reply_text("Failed to download the file. Please try again.")
            return

        caption = msg.caption or ""

        if media_type == "image":
            body = caption if caption else "Please analyze this image."
            prompt = f"[The user sent an image: {dest}]\n{body}"
        else:
            body = caption if caption else "Please analyze this file."
            prompt = f"[The user sent a file: {dest}]\n{body}"

        try:
            position = await self.sm.send_prompt(session.id, prompt)
        except RuntimeError as e:
            await update.message.reply_text(str(e))
            return

        prefix = self._session_prefix(session, str(update.effective_user.id))
        auto_note = "(auto-activated) " if auto_selected else ""
        kind = "Image" if media_type == "image" else "File"

        if position == 0:
            await update.message.reply_text(
                f"{prefix}{auto_note}<i>{kind} received, processing...</i>",
                parse_mode="HTML",
            )
            self._start_typing(update.effective_chat.id, session.id)
        else:
            await update.message.reply_text(
                f"{prefix}{auto_note}{kind} queued (position {position})."
            )

        self._prompt_message_ids[session.id] = update.message.message_id

    async def _handle_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Reply to unsupported message types."""
        if not self._authorized(update):
            return
        await update.message.reply_text("Unsupported message type. Send text, images, or files.")

    # -- lifecycle ----------------------------------------------------------

    async def notify(self, chat_id: int, text: str) -> None:
        """Send a direct notification message to a chat (bypasses session routing)."""
        await self._send_chunked(chat_id, text)

    async def start(self) -> None:
        self.engine.services["telegram"] = self
        self.sm.add_delivery_callback(self._deliver_result)
        self._user_stream_levels = self._load_stream_levels()

        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("new", self._cmd_new))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("switch", self._cmd_switch))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("delete", self._cmd_delete))
        self.app.add_handler(CommandHandler("verbose", self._cmd_verbose))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback_query))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        self.app.add_handler(
            MessageHandler(
                (filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
                self._handle_media,
            )
        )
        self.app.add_handler(
            MessageHandler(
                ~filters.TEXT & ~filters.COMMAND & ~filters.PHOTO & ~filters.Document.ALL,
                self._handle_unsupported,
            )
        )

        await self.app.initialize()
        await self.app.bot.set_my_commands(
            [
                BotCommand("start", "Show welcome message"),
                BotCommand("new", "Create new session [name] [dir]"),
                BotCommand("sessions", "List all sessions"),
                BotCommand("switch", "Switch active session"),
                BotCommand("status", "Auth, usage & session info"),
                BotCommand("stop", "Stop running task + clear queue"),
                BotCommand("delete", "Delete a session"),
                BotCommand("verbose", "Tool verbosity: all/mcp/none"),
            ]
        )
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started polling.")

    async def stop(self) -> None:
        # Cancel all typing indicator tasks
        for task in self._typing_tasks.values():
            if not task.done():
                task.cancel()
        self._typing_tasks.clear()

        # Cancel all pending stream flush and tool flush tasks
        for buf in self._stream_buffers.values():
            if buf.flush_task and not buf.flush_task.done():
                buf.flush_task.cancel()
            if buf.tool_flush_task and not buf.tool_flush_task.done():
                buf.tool_flush_task.cancel()
        self._stream_buffers.clear()
        self._awaiting_text_answer.clear()
        self._prompt_message_ids.clear()

        self.engine.services.pop("telegram", None)

        if self.app:
            try:
                await asyncio.wait_for(self.app.updater.stop(), timeout=5.0)
            except Exception:
                log.warning("Telegram updater.stop() failed/timed out", exc_info=True)
            try:
                await self.app.stop()
            except Exception:
                log.warning("Telegram app.stop() failed", exc_info=True)
            try:
                await self.app.shutdown()
            except Exception:
                log.warning("Telegram app.shutdown() failed", exc_info=True)
            log.info("Telegram bot stopped.")

    async def health_check(self) -> dict:
        polling = self.app is not None and self.app.updater is not None and self.app.updater.running
        result: dict = {
            "status": "ok" if polling else "error",
            "polling": polling,
        }
        if self.allowed_users:
            result["policies"] = {
                "allowed_users": len(self.allowed_users),
            }
        return result
