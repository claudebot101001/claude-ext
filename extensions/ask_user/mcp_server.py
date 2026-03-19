#!/usr/bin/env python3
"""Ask-user MCP server — provides the ``ask_user`` tool to Claude sessions.

Spawned per Claude session by SessionManager.  Uses the bridge reverse
channel to call back into the main process where PendingStore + delivery
pipeline handle the actual user interaction.
"""

import os
import sys
from pathlib import Path

# Ensure the project root is importable
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class AskUserMCPServer(MCPServerBase):
    name = "ask_user"
    tools = [
        {
            "name": "ask_user",
            "description": (
                "Ask the user a question and wait for their response. "
                "Use this when you need clarification, confirmation, or a "
                "choice from the user before proceeding. Optionally provide "
                "a list of options for the user to pick from."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of choices for the user. "
                            "If omitted, user provides free-text input."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "ask_user": self._handle_ask_user,
        }

    def _handle_ask_user(self, args: dict) -> str:
        question = args.get("question", "")
        options = args.get("options") or []

        if not question:
            return "Error: 'question' is required."

        if not self.bridge:
            return "Error: Bridge not available. Cannot reach user."

        timeout = float(os.environ.get("ASK_USER_TIMEOUT", "300"))

        try:
            result = self.bridge.call(
                "ask_user",
                {
                    "session_id": self.session_id,
                    "question": question,
                    "options": options,
                },
                # Bridge timeout slightly longer than PendingStore timeout
                # so the store times out first and returns a clean response
                timeout=timeout + 10,
            )
        except TimeoutError:
            return "User did not respond in time."
        except (ConnectionError, RuntimeError) as e:
            return f"Error reaching user: {e}"

        if result.get("timed_out"):
            return "User did not respond in time."
        if result.get("cancelled"):
            return "Question was cancelled (session stopped)."
        if result.get("error"):
            return f"Error: {result['error']}"

        answer = result.get("answer", "")
        if answer:
            return f"User's response: {answer}"
        return "User provided an empty response."


if __name__ == "__main__":
    AskUserMCPServer().run()
