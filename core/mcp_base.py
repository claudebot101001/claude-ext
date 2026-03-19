"""MCP stdio server base class.

Extracts the JSON-RPC protocol boilerplate so that extension MCP servers
only need to declare ``tools`` (schema) and ``handlers`` (business logic).

Supports **gateway mode**: when ``CLAUDE_EXT_GATEWAY_MODE=1``, servers with
more than one tool consolidate into a single gateway tool, reducing token
overhead from ~6,000 to ~1,000 per API call.

Zero external dependencies — stdlib only.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path


class MCPServerBase:
    """Base class for MCP stdio servers spawned per Claude session.

    Subclasses set ``name``, ``version``, ``tools`` (class attr) and
    populate ``self.handlers`` in ``__init__`` with tool-name → callable
    mappings.  The callable receives ``(args: dict) -> str``.

    For gateway mode, subclasses may set ``gateway_description`` to provide
    a one-liner for the consolidated tool schema.
    """

    name: str = "unnamed"
    version: str = "1.0.0"
    tools: list[dict] = []
    gateway_description: str = ""

    def __init__(self):
        self.handlers: dict[str, Callable[[dict], str]] = {}
        self._bridge = _UNSET  # lazy sentinel
        self._gateway_mode = os.environ.get("CLAUDE_EXT_GATEWAY_MODE") == "1"

    # -- environment properties (injected by SessionManager) ----------------

    @property
    def session_id(self) -> str:
        return os.environ.get("CLAUDE_EXT_SESSION_ID", "")

    @property
    def state_dir(self) -> str:
        return os.environ.get("CLAUDE_EXT_STATE_DIR", "")

    @property
    def session_user_id(self) -> str:
        return os.environ.get("CLAUDE_EXT_USER_ID", "")

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

    # -- gateway mode helpers -----------------------------------------------

    def _is_gateway_active(self) -> bool:
        """True if gateway mode is on AND this server has >1 tool."""
        return self._gateway_mode and len(self.tools) > 1

    def _gateway_tool_schema(self) -> dict:
        """Return the single consolidated gateway tool definition."""
        desc = (
            self.gateway_description
            or f"{self.name} extension. Call with action='help' for available commands."
        )
        return {
            "name": self.name,
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Command to execute. Use 'help' to list all available commands.",
                    },
                    "params": {
                        "type": "object",
                        "description": "Parameters for the command (see action='help' for details).",
                        "default": {},
                    },
                },
                "required": ["action"],
            },
        }

    def _generate_help(self) -> str:
        """Build markdown help text from self.tools schemas."""
        lines = [f"# {self.name} — available commands\n"]
        for tool in self.tools:
            action = tool["name"]
            desc = tool.get("description", "")
            lines.append(f"## {action}")
            if desc:
                lines.append(desc)

            schema = tool.get("inputSchema", {})
            props = schema.get("properties", {})
            required = set(schema.get("required", []))

            if props:
                lines.append("\n**Parameters:**")
                for pname, pschema in props.items():
                    req = " (required)" if pname in required else ""
                    ptype = pschema.get("type", "any")
                    pdesc = pschema.get("description", "")
                    lines.append(f"- `{pname}` ({ptype}{req}): {pdesc}")
            lines.append("")
        return "\n".join(lines)

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
            if self._is_gateway_active():
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": [self._gateway_tool_schema()]},
                }
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.tools},
            }

        if method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            # Gateway dispatch: tool_name matches server name
            if self._is_gateway_active() and tool_name == self.name:
                action = arguments.get("action", "")
                action_params = arguments.get("params", {})
                # Claude sometimes serializes params as a JSON string
                if isinstance(action_params, str):
                    try:
                        action_params = json.loads(action_params)
                    except (json.JSONDecodeError, TypeError):
                        action_params = {}

                if action == "help":
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": self._generate_help()}],
                        },
                    }

                handler = self.handlers.get(action)
                if not handler:
                    available = ", ".join(sorted(self.handlers.keys()))
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Unknown action: {action}. Available: {available}. Use action='help' for details.",
                                }
                            ],
                            "isError": True,
                        },
                    }

                try:
                    result_text = handler(action_params)
                except Exception as e:
                    result_text = f"Error: {e}"

                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                    },
                }

            # Normal dispatch: direct tool name match
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
