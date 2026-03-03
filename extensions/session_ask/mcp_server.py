#!/usr/bin/env python3
"""Session-ask MCP server — provides cross-session RPC tools.

Spawned per Claude session by SessionManager.  All operations delegate
to the main process via bridge RPC.
"""

import os
import sys
from pathlib import Path

# Ensure the project root is importable
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class SessionAskMCPServer(MCPServerBase):
    name = "session_ask"
    version = "1.0.0"
    gateway_description = "Cross-session communication (ask/reply/list). action='help' for details."
    tools = [
        {
            "name": "session_ask",
            "description": (
                "Send a question to another session and BLOCK until it replies. "
                "The target session will receive your question as a prompt and must "
                "use session_reply to respond. Use session_list to discover sessions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_session_id": {
                        "type": "string",
                        "description": (
                            "The session ID to send the question to. "
                            "Use session_list to find available sessions."
                        ),
                    },
                    "question": {
                        "type": "string",
                        "description": "The question to ask the target session",
                    },
                },
                "required": ["target_session_id", "question"],
            },
        },
        {
            "name": "session_reply",
            "description": (
                "Reply to an inter-session question. Use this when you receive "
                "an 'Inter-Session Request' prompt. You MUST include the "
                "request_id from the question."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "The request_id from the Inter-Session Request",
                    },
                    "reply": {
                        "type": "string",
                        "description": "Your reply to the question",
                    },
                },
                "required": ["request_id", "reply"],
            },
        },
        {
            "name": "session_list",
            "description": (
                "List all active sessions for the current user. "
                "Shows session IDs, names, slots, and status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "session_ask": self._handle_ask,
            "session_reply": self._handle_reply,
            "session_list": self._handle_list,
        }

    def _bridge_call(self, method: str, extra_params: dict, timeout: float = 60) -> dict:
        """Call bridge with session_id injected."""
        if not self.bridge:
            return {"error": "Bridge not available"}
        params = {"session_id": self.session_id}
        params.update(extra_params)
        try:
            return self.bridge.call(method, params, timeout=timeout)
        except TimeoutError:
            return {"error": "Request timed out"}
        except (ConnectionError, RuntimeError) as e:
            return {"error": f"Bridge error: {e}"}

    # -- handlers -------------------------------------------------------------

    def _handle_ask(self, args: dict) -> str:
        target = args.get("target_session_id", "")
        question = args.get("question", "")

        if not target:
            return "Error: target_session_id is required."
        if not question:
            return "Error: question is required."

        timeout = float(os.environ.get("SESSION_ASK_TIMEOUT", "300"))

        result = self._bridge_call(
            "session_ask",
            {
                "target_session_id": target,
                "question": question,
            },
            # Bridge timeout > PendingStore timeout so store times out first
            timeout=timeout + 10,
        )

        if result.get("timed_out"):
            return "Target session did not reply in time."
        if result.get("cancelled"):
            return "Request was cancelled (session stopped)."
        if result.get("error"):
            return f"Error: {result['error']}"

        reply = result.get("reply", "")
        if reply:
            return f"Reply from target session:\n\n{reply}"
        return "Target session provided an empty reply."

    def _handle_reply(self, args: dict) -> str:
        request_id = args.get("request_id", "")
        reply = args.get("reply", "")

        if not request_id:
            return "Error: request_id is required."
        if not reply:
            return "Error: reply is required."

        result = self._bridge_call(
            "session_reply",
            {
                "request_id": request_id,
                "reply": reply,
            },
        )

        if result.get("error"):
            return f"Error: {result['error']}"
        if result.get("resolved"):
            return "Reply sent successfully."
        return "Unexpected response from bridge."

    def _handle_list(self, args: dict) -> str:
        result = self._bridge_call("session_list", {})

        if result.get("error"):
            return f"Error: {result['error']}"

        sessions = result.get("sessions", [])
        if not sessions:
            return "No active sessions."

        lines = [f"{len(sessions)} session(s):"]
        for s in sessions:
            self_marker = " (YOU)" if s.get("is_self") else ""
            lines.append(
                f'- #{s.get("slot", "?")} "{s.get("name", "")}" '
                f"[{s.get('status', 'unknown')}] "
                f"session_id: {s.get('session_id', '')}"
                f"{self_marker}"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    SessionAskMCPServer().run()
