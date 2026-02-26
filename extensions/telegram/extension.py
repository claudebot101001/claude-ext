"""Telegram bot extension - bridges Telegram messages to Claude Code."""

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.extension import Extension
from core.status import format_status, get_auth_info, get_usage

log = logging.getLogger(__name__)


class ExtensionImpl(Extension):
    name = "telegram"

    def configure(self, engine, config):
        super().configure(engine, config)
        self.token = config["token"]
        self.allowed_users = set(config.get("allowed_users", []))
        self.working_dir = config.get("working_dir")
        self.app: Application | None = None

    def _authorized(self, update: Update) -> bool:
        """Check if the user is in the allow list. Empty list = allow all."""
        if not self.allowed_users:
            return True
        user = update.effective_user
        return (
            user is not None
            and (user.id in self.allowed_users or user.username in self.allowed_users)
        )

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "Claude Code bridge ready. Send me any message.\n\n"
            "Commands:\n"
            "/status - Show session & usage info\n"
            "/new - Reset session"
        )

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset session - next message won't use --continue."""
        if not self._authorized(update):
            return
        ctx.user_data["continue"] = False
        await update.message.reply_text("Session reset. Next message starts fresh.")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show auth, usage quota, and current session stats."""
        if not self._authorized(update):
            return

        await update.message.chat.send_action("typing")

        auth, usage = await get_auth_info(), await get_usage()
        session = self.engine.last_session if self.engine.last_session else None
        text = format_status(auth, usage, session)

        await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        text = update.message.text
        if not text:
            return

        await update.message.chat.send_action("typing")

        continue_session = ctx.user_data.get("continue", False)
        response = await self.engine.ask(
            prompt=text,
            cwd=self.working_dir,
            continue_session=continue_session,
        )
        ctx.user_data["continue"] = True

        # Telegram has a 4096 char limit per message
        for i in range(0, len(response), 4000):
            chunk = response[i : i + 4000]
            await update.message.reply_text(chunk)

    async def start(self) -> None:
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("new", self._cmd_new))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        await self.app.initialize()
        await self.app.bot.set_my_commands([
            BotCommand("start", "Show welcome message"),
            BotCommand("status", "Session & usage info"),
            BotCommand("new", "Reset session"),
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
