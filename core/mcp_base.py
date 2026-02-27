"""MCP stdio server base class.

Extracts the JSON-RPC protocol boilerplate so that extension MCP servers
only need to declare ``tools`` (schema) and ``handlers`` (business logic).

Zero external dependencies — stdlib only.
"""

import json
import os
import sys
from pathlib import Path
from typing import Callable


class MCPServerBase:
    """Base class for MCP stdio servers spawned per Claude session.

    Subclasses set ``name``, ``version``, ``tools`` (class attr) and
    populate ``self.handlers`` in ``__init__`` with tool-name → callable
    mappings.  The callable receives ``(args: dict) -> str``.
    """

    name: str = "unnamed"
    version: str = "1.0.0"
    tools: list[dict] = []

    def __init__(self):
        self.handlers: dict[str, Callable[[dict], str]] = {}
        self._bridge = _UNSET  # lazy sentinel

    # -- environment properties (injected by SessionManager) ----------------

    @property
    def session_id(self) -> str:
        return os.environ.get("CLAUDE_EXT_SESSION_ID", "")

    @property
    def state_dir(self) -> str:
        return os.environ.get("CLAUDE_EXT_STATE_DIR", "")

    def session_context(self) -> dict:
        """Read the current session's state.json."""
        if not self.state_dir:
            return {}
        state_file = Path(self.state_dir) / "state.json"
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    # -- optional: reverse channel via bridge --------------------------------

    @property
    def bridge(self):
        """Lazy-init BridgeClient if CLAUDE_EXT_BRIDGE_SOCKET is set."""
        if self._bridge is _UNSET:
            sock_path = os.environ.get("CLAUDE_EXT_BRIDGE_SOCKET", "")
            if sock_path:
                from core.bridge import BridgeClient
                self._bridge = BridgeClient(sock_path)
            else:
                self._bridge = None
        return self._bridge

    # -- JSON-RPC protocol --------------------------------------------------

    @staticmethod
    def _write_msg(msg: dict) -> None:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    def _handle_message(self, msg: dict) -> dict | None:
        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            }

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.tools},
            }

        if method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            handler = self.handlers.get(tool_name)
            if not handler:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                        "isError": True,
                    },
                }

            try:
                result_text = handler(arguments)
            except Exception as e:
                result_text = f"Error: {e}"

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                },
            }

        # Unknown method
        if msg_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return None

    def run(self) -> None:
        """Read JSON-RPC messages from stdin, respond on stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = self._handle_message(msg)
            if response is not None:
                self._write_msg(response)

        # Cleanup bridge on exit
        if self._bridge is not _UNSET and self._bridge is not None:
            self._bridge.close()


# Sentinel for lazy init (distinguish "not yet checked" from None)
_UNSET = object()
