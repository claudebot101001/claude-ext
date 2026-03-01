"""Ask-user extension — lets Claude ask the user a question mid-session.

Registers an MCP server (``ask_user`` tool) and a bridge handler.  When
Claude calls the tool the MCP server issues a bridge RPC that blocks until
the user responds (via Telegram inline button, text reply, etc.) or times
out.

Data flow:
  Claude → MCP tool → bridge.call("ask_user") → BridgeServer handler
    → PendingStore.register + SessionManager.deliver(is_question)
    → [user answers via Telegram or other frontend]
    → PendingStore.resolve → bridge returns → MCP tool returns → Claude continues
"""

import asyncio
import logging
import sys
from pathlib import Path

from core.extension import Extension

log = logging.getLogger(__name__)


class ExtensionImpl(Extension):
    name = "ask_user"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._timeout = float(config.get("timeout", 300))

    @property
    def sm(self):
        return self.engine.session_manager

    async def start(self) -> None:
        # Register MCP server so every Claude session gets the ask_user tool
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server(
            "ask_user",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {"ASK_USER_TIMEOUT": str(self._timeout)},
            },
            tools=[
                {
                    "name": "ask_user",
                    "description": "Ask the user a question and wait for response",
                },
            ],
        )

        # Register bridge handler for reverse-channel calls from MCP server
        self.engine.bridge.add_handler(self._bridge_handler)

        # Disable built-in AskUserQuestion — our MCP ask_user replaces it
        self.sm.register_disallowed_tool("AskUserQuestion")

        # Minimal guidance — tool routing is enforced by --disallowedTools
        self.sm.add_system_prompt(
            "When you need to ask the user a question or present choices, "
            "use the ask_user MCP tool.",
            mcp_server="ask_user",
        )

        log.info("ask_user extension started (timeout=%ss)", self._timeout)

    async def stop(self) -> None:
        log.info("ask_user extension stopped.")

    async def health_check(self) -> dict:
        return {"status": "ok"}

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        """Handle 'ask_user' bridge RPCs from the MCP server process."""
        if method != "ask_user":
            return None  # not ours — let other handlers try

        session_id = params.get("session_id", "")
        question = params.get("question", "")
        options = params.get("options") or []

        if not session_id or session_id not in self.sm.sessions:
            return {"answer": "", "error": "Invalid session_id"}

        pending = self.engine.pending

        # 1. Register a pending entry
        entry = pending.register(
            session_id=session_id,
            data={"question": question, "options": options},
            timeout=self._timeout,
        )

        # 2. Deliver the question through the normal delivery pipeline
        await self.sm.deliver(
            session_id,
            question,
            {
                "is_question": True,
                "request_id": entry.key,
                "options": options,
            },
        )

        # 3. Block until user responds, times out, or is cancelled
        try:
            answer = await pending.wait(entry.key)
            return {"answer": str(answer)}
        except TimeoutError:
            return {"answer": "", "timed_out": True}
        except asyncio.CancelledError:
            return {"answer": "", "cancelled": True}
