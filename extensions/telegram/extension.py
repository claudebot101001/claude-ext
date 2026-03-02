"""Telegram bot extension - multi-session bridge to Claude Code via tmux."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

log = logging.getLogger(__name__)

MAX_TG_MESSAGE = 4000  # Telegram limit is 4096; leave margin
STREAM_FLUSH_DELAY = 2.0  # seconds to wait before flushing text buffer


@dataclass
class _StreamBuffer:
    """Per-session buffer for debouncing stream text events."""

    chat_id: int
    slot: int
    name: str
    text_parts: list[str] = field(default_factory=list)
    flush_task: asyncio.Task | None = None


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
            prefix = f"[#{session.slot} {session.name}] "
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
            text = f"[#{session.slot} {session.name}] Still working... ({mins}m elapsed)"
            await self._send_chunked(chat_id, text)
            return

        # --- Stream: text event (debounced) ---
        if metadata.get("is_stream") and metadata.get("stream_type") == "text":
            buf = self._stream_buffers.get(session_id)
            if not buf:
                buf = _StreamBuffer(chat_id=chat_id, slot=session.slot, name=session.name)
                self._stream_buffers[session_id] = buf
            buf.text_parts.append(result_text)
            # Reset flush timer
            if buf.flush_task and not buf.flush_task.done():
                buf.flush_task.cancel()
            buf.flush_task = asyncio.create_task(self._delayed_flush(session_id))
            return

        # --- Stream: tool_use event (immediate) ---
        if metadata.get("is_stream") and metadata.get("stream_type") == "tool_use":
            await self._flush_stream_buffer(session_id)
            summary = self._format_tool_use(metadata)
            prefix = f"[#{session.slot} {session.name}] "
            await self._send_chunked(chat_id, f"{prefix}{summary}")
            return

        # --- Stopped ---
        if metadata.get("is_stopped"):
            await self._flush_stream_buffer(session_id)
            prefix = f"[#{session.slot} {session.name}] "
            await self._send_chunked(chat_id, f"{prefix}Task stopped.")
            return

        # --- Final result (streaming mode: text already delivered) ---
        if metadata.get("is_final"):
            flushed = await self._flush_stream_buffer(session_id)
            self._stream_buffers.pop(session_id, None)

            prefix = f"[#{session.slot} {session.name}] "

            # Fallback: if stream buffer was empty (text events were lost
            # during debounce, or never delivered), use result_text from
            # _parse_stream_result as a safety net.
            if not flushed and result_text and not metadata.get("is_error"):
                log.warning(
                    "Stream text was not buffered for session %s, using fallback",
                    session_id[:8],
                )
                await self._send_chunked(chat_id, f"{prefix}{result_text}")

            cost = metadata.get("total_cost_usd")
            turns = metadata.get("num_turns")
            if metadata.get("is_error"):
                err_text = result_text or "[Error]"
                await self._send_chunked(chat_id, f"{prefix}{err_text}")
            elif cost is not None:
                await self._send_chunked(chat_id, f"{prefix}--- ${cost:.4f} | {turns} turns ---")
            return

        # --- Fallback (recovery with full text, backward compat) ---
        if result_text:
            prefix = f"[#{session.slot} {session.name}] "
            cost = metadata.get("total_cost_usd")
            turns = metadata.get("num_turns")
            footer = ""
            if cost is not None and not metadata.get("is_error"):
                footer = f"\n\n--- ${cost:.4f} | {turns} turns ---"
            await self._send_chunked(chat_id, f"{prefix}{result_text}{footer}")

    # -- stream buffer helpers -----------------------------------------------

    async def _delayed_flush(self, session_id: str) -> None:
        """Wait then flush the text buffer for a session."""
        await asyncio.sleep(STREAM_FLUSH_DELAY)
        await self._flush_stream_buffer(session_id)

    async def _flush_stream_buffer(self, session_id: str) -> bool:
        """Send accumulated text in the stream buffer, if any.

        Returns True if text was flushed (regardless of send success).
        """
        buf = self._stream_buffers.get(session_id)
        if not buf or not buf.text_parts:
            return False
        # Cancel pending flush timer
        if buf.flush_task and not buf.flush_task.done():
            buf.flush_task.cancel()
        text = "".join(buf.text_parts)
        buf.text_parts.clear()
        buf.flush_task = None
        if text.strip():
            prefix = f"[#{buf.slot} {buf.name}] "
            await self._send_chunked(buf.chat_id, f"{prefix}{text}")
            return True
        return False

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

    async def _send_chunked(self, chat_id: int, text: str) -> None:
        """Send text in chunks, splitting at newline boundaries when possible."""
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
                await self.app.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception:
                log.exception("Failed to deliver message to chat %s", chat_id)
                break  # subsequent chunks will almost certainly fail too

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
            "/delete <slot|name> - Delete session\n\n"
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

        # Cancel any pending ask_user questions for this session
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

        # Clean up stream buffer
        buf = self._stream_buffers.pop(session_id, None)
        if buf and buf.flush_task and not buf.flush_task.done():
            buf.flush_task.cancel()

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
            # resolve() returned False = question expired, fall through

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

        tag = f"[#{session.slot} {session.name}]"
        auto_note = " (auto-activated)" if auto_selected else ""

        if position == 0:
            await update.message.reply_text(f"{tag}{auto_note} Processing...")
        else:
            await update.message.reply_text(
                f"{tag}{auto_note} Queued (position {position}). "
                f"Use /new to start a parallel session."
            )

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

        tag = f"[#{session.slot} {session.name}]"
        auto_note = " (auto-activated)" if auto_selected else ""
        kind = "Image" if media_type == "image" else "File"

        if position == 0:
            await update.message.reply_text(f"{tag}{auto_note} {kind} received, processing...")
        else:
            await update.message.reply_text(
                f"{tag}{auto_note} {kind} queued (position {position})."
            )

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

        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("new", self._cmd_new))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("switch", self._cmd_switch))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("delete", self._cmd_delete))
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
            ]
        )
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started polling.")

    async def stop(self) -> None:
        # Cancel all pending stream flush tasks
        for buf in self._stream_buffers.values():
            if buf.flush_task and not buf.flush_task.done():
                buf.flush_task.cancel()
        self._stream_buffers.clear()
        self._awaiting_text_answer.clear()

        self.engine.services.pop("telegram", None)

        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
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
