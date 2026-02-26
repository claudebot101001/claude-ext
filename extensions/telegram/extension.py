"""Telegram bot extension - multi-session bridge to Claude Code via tmux."""

import logging
import os

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.extension import Extension
from core.session import SessionStatus
from core.status import format_status, get_auth_info, get_usage

log = logging.getLogger(__name__)

MAX_TG_MESSAGE = 4000  # Telegram limit is 4096; leave margin


class ExtensionImpl(Extension):
    name = "telegram"

    def configure(self, engine, config):
        super().configure(engine, config)
        self.token = config["token"]
        self.allowed_users = set(config.get("allowed_users", []))
        self.working_dir = config.get("working_dir") or os.getcwd()
        self.app: Application | None = None

    def _authorized(self, update: Update) -> bool:
        if not self.allowed_users:
            return True
        user = update.effective_user
        return (
            user is not None
            and (user.id in self.allowed_users or user.username in self.allowed_users)
        )

    @property
    def sm(self):
        return self.engine.session_manager

    # -- delivery callback (called by SessionManager) -----------------------

    async def _deliver_result(
        self,
        session_id: str,
        user_id: int,
        chat_id: int,
        result_text: str,
        metadata: dict,
    ) -> None:
        # Heartbeat messages are plain text, no decoration
        if metadata.get("is_heartbeat"):
            await self._send_chunked(chat_id, result_text)
            return

        session = self.sm.sessions.get(session_id)
        prefix = f"[#{session.slot} {session.name}] " if session else ""

        cost = metadata.get("total_cost_usd")
        turns = metadata.get("num_turns")
        footer = ""
        if cost is not None and not metadata.get("is_error"):
            footer = f"\n\n--- ${cost:.4f} | {turns} turns ---"

        text = f"{prefix}{result_text}{footer}"
        await self._send_chunked(chat_id, text)

    async def _send_chunked(self, chat_id: int, text: str) -> None:
        for i in range(0, len(text), MAX_TG_MESSAGE):
            chunk = text[i : i + MAX_TG_MESSAGE]
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception:
                log.exception("Failed to deliver message to chat %s", chat_id)

    # -- helpers ------------------------------------------------------------

    def _resolve_session(self, user_id: int, target: str):
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

    async def _ensure_active_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Return the active session, creating one if needed."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        active_id = ctx.user_data.get("active_session_id")

        # Check if the stored active session still exists
        if active_id and active_id not in self.sm.sessions:
            active_id = None
            ctx.user_data["active_session_id"] = None

        if not active_id:
            # Auto-pick an existing idle session or create a new one
            user_sessions = self.sm.get_sessions_for_user(user_id)
            if user_sessions:
                session = user_sessions[0]
            else:
                session = await self.sm.create_session(
                    name="default",
                    user_id=user_id,
                    chat_id=chat_id,
                    working_dir=self.working_dir,
                )
            ctx.user_data["active_session_id"] = session.id
            active_id = session.id

        return self.sm.sessions[active_id]

    # -- command handlers ---------------------------------------------------

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        max_s = self.sm.max_sessions_per_user
        await update.message.reply_text(
            "Claude Code bridge ready.\n\n"
            "Commands:\n"
            f"/new [name] - Create new session (max {max_s})\n"
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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # Parse: /new [name] [working_dir]
        parts = update.message.text.split(maxsplit=2)
        name = None
        work_dir = self.working_dir

        if len(parts) >= 2:
            name = parts[1].strip()
        if len(parts) >= 3:
            candidate = parts[2].strip()
            if os.path.isdir(candidate):
                work_dir = candidate
            else:
                await update.message.reply_text(f"Directory not found: {candidate}")
                return

        # Auto-name based on slot if not given
        if not name:
            # Preview which slot will be used
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
                chat_id=chat_id,
                working_dir=work_dir,
            )
        except RuntimeError as e:
            await update.message.reply_text(str(e))
            return

        ctx.user_data["active_session_id"] = session.id
        dir_note = f"\nDir: {work_dir}" if work_dir != self.working_dir else ""
        await update.message.reply_text(
            f"Created #{session.slot} '{session.name}'. Now active.{dir_note}"
        )

    async def _cmd_sessions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        user_id = update.effective_user.id
        active_id = ctx.user_data.get("active_session_id")

        sessions = self.sm.get_sessions_for_user(user_id)
        if not sessions:
            await update.message.reply_text("No sessions. Send a message to create one.")
            return

        lines = ["Sessions (* = active):"]
        for s in sessions:
            marker = " *" if s.id == active_id else ""
            status_tag = s.status.value.upper()
            prompt_info = ""
            if s.last_prompt:
                prompt_info = f"\n      {s.last_prompt[:50]}"
            lines.append(f"  #{s.slot}. {s.name} [{status_tag}]{marker}{prompt_info}")

        await update.message.reply_text("\n".join(lines))

    async def _cmd_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("Usage: /switch <slot number or name>")
            return

        target = args[1].strip()
        found = self._resolve_session(update.effective_user.id, target)

        if not found:
            await update.message.reply_text(f"Session '{target}' not found.")
            return

        ctx.user_data["active_session_id"] = found.id
        await update.message.reply_text(
            f"Switched to #{found.slot} '{found.name}' [{found.status.value}]."
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.chat.send_action("typing")

        active_id = ctx.user_data.get("active_session_id")
        session_meta = None
        if active_id and active_id in self.sm.sessions:
            session_meta = self.sm.sessions[active_id].last_result_metadata or None

        auth, usage = await get_auth_info(), await get_usage()
        text = format_status(auth, usage, session_meta)
        await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        active_id = ctx.user_data.get("active_session_id")
        if not active_id or active_id not in self.sm.sessions:
            await update.message.reply_text("No active session.")
            return

        session = self.sm.sessions[active_id]
        if session.status != SessionStatus.BUSY:
            await update.message.reply_text(
                f"#{session.slot} '{session.name}' is not running."
            )
            return

        await self.sm.stop_session(active_id)
        await update.message.reply_text(
            f"Stopping #{session.slot} '{session.name}' (queue cleared)..."
        )

    async def _cmd_delete(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        user_id = update.effective_user.id
        args = update.message.text.split(maxsplit=1)

        if len(args) < 2:
            await update.message.reply_text("Usage: /delete <slot number or name>")
            return

        target = args[1].strip()
        found = self._resolve_session(user_id, target)
        if not found:
            await update.message.reply_text(f"Session '{target}' not found.")
            return

        slot = found.slot
        name = found.name
        await self.sm.destroy_session(found.id)

        if ctx.user_data.get("active_session_id") == found.id:
            ctx.user_data["active_session_id"] = None

        await update.message.reply_text(f"Deleted #{slot} '{name}'.")

    # -- message handler ----------------------------------------------------

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = update.message.text
        if not text:
            return

        session = await self._ensure_active_session(update, ctx)

        # Dead sessions need manual intervention
        if session.status == SessionStatus.DEAD:
            await update.message.reply_text(
                f"#{session.slot} '{session.name}' is dead. "
                f"Use /delete {session.slot} and /new to start fresh."
            )
            return

        # Update chat_id in case it changed
        session.chat_id = update.effective_chat.id

        position = await self.sm.send_prompt(session.id, text)

        if position == 0:
            await update.message.reply_text(
                f"[#{session.slot} {session.name}] Processing..."
            )
        else:
            await update.message.reply_text(
                f"[#{session.slot} {session.name}] Queued (position {position}). "
                f"Use /new to start a parallel session."
            )

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.sm.set_delivery_callback(self._deliver_result)

        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("new", self._cmd_new))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("switch", self._cmd_switch))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("delete", self._cmd_delete))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        await self.app.initialize()
        await self.app.bot.set_my_commands([
            BotCommand("start", "Show welcome message"),
            BotCommand("new", "Create new session [name] [dir]"),
            BotCommand("sessions", "List all sessions"),
            BotCommand("switch", "Switch active session"),
            BotCommand("status", "Auth, usage & session info"),
            BotCommand("stop", "Stop running task + clear queue"),
            BotCommand("delete", "Delete a session"),
        ])
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started polling.")

    async def stop(self) -> None:
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            log.info("Telegram bot stopped.")
