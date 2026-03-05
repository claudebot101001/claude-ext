#!/usr/bin/env python3
"""Context management MCP server — token tracking and compaction control.

Spawned by Claude Code per session. Uses gateway mode (single tool, multiple actions).
"""

import json
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class ContextMCPServer(MCPServerBase):
    name = "context"
    gateway_description = (
        "Context window management (status/compact/configure). action='help' for details."
    )
    tools = [
        {
            "name": "context_status",
            "description": (
                "Get current context window usage for this session. "
                "Returns token counts, fill percentage, and compaction history."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "context_compact",
            "description": (
                "Trigger context compaction for this session. "
                "Queues a /compact command; costs API tokens."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "context_configure",
            "description": "Configure auto-compaction for this session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "auto_compact": {
                        "type": "boolean",
                        "description": "Enable or disable auto-compaction",
                    },
                    "threshold_pct": {
                        "type": "integer",
                        "description": "Fill percentage threshold to trigger compaction (default: 85)",
                    },
                },
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "context_status": self._handle_status,
            "context_compact": self._handle_compact,
            "context_configure": self._handle_configure,
        }

    def _handle_status(self, args: dict) -> str:
        result = self.bridge.call("context_status", {"session_id": self.session_id})
        return json.dumps(result, indent=2)

    def _handle_compact(self, args: dict) -> str:
        result = self.bridge.call("context_compact", {"session_id": self.session_id})
        return json.dumps(result)

    def _handle_configure(self, args: dict) -> str:
        params = {
            "session_id": self.session_id,
        }
        if "auto_compact" in args:
            params["auto_compact"] = args["auto_compact"]
        if "threshold_pct" in args:
            params["threshold_pct"] = args["threshold_pct"]
        result = self.bridge.call("context_configure", params)
        return json.dumps(result)


if __name__ == "__main__":
    ContextMCPServer().run()
