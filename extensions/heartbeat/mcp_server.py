#!/usr/bin/env python3
"""Heartbeat MCP server — manage autonomous heartbeat via Claude tool calls.

Spawned by Claude Code per session.  Inherits MCPServerBase for protocol
handling; uses direct file I/O via HEARTBEAT_DIR environment variable.
"""

import json
import os
import shlex
import sys
from pathlib import Path

# Ensure the project root is importable (mcp_server.py lives in extensions/heartbeat/)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402
from extensions.heartbeat.store import HeartbeatStore  # noqa: E402


class HeartbeatMCPServer(MCPServerBase):
    name = "heartbeat"
    tools = [
        {
            "name": "heartbeat_get_instructions",
            "description": (
                "Read the heartbeat standing instructions (HEARTBEAT.md). "
                "This defines what the autonomous heartbeat checks periodically."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "heartbeat_set_instructions",
            "description": (
                "Overwrite the heartbeat standing instructions (HEARTBEAT.md). "
                "Use this to add, modify, or remove periodic monitoring tasks."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Full content for HEARTBEAT.md",
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "heartbeat_get_status",
            "description": (
                "Get the heartbeat scheduler status: enabled state, run counts, "
                "next scheduled run, consecutive idle count."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "heartbeat_pause",
            "description": "Pause the autonomous heartbeat. It will not run until resumed.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "heartbeat_resume",
            "description": "Resume the autonomous heartbeat after pausing.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "heartbeat_trigger",
            "description": (
                "Submit an event to trigger a heartbeat check. "
                "Use 'immediate' urgency to wake the scheduler now."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Event category (e.g. 'deployment_done', 'price_alert')",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["immediate", "normal"],
                        "description": "immediate: wake scheduler now. normal: process on next timer cycle.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Optional event data",
                    },
                },
                "required": ["event_type"],
            },
        },
        {
            "name": "heartbeat_get_trigger_command",
            "description": (
                "Get a shell command that triggers the heartbeat from any external process. "
                "Use this to set up post-completion hooks or monitoring scripts that wake "
                "the heartbeat when a condition is met, even after the current session ends. "
                "Example: chain with 'rsync ... && <returned_command>' and run in background."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Event category (e.g. 'transfer_done', 'cpi_released')",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["immediate", "normal"],
                        "description": "immediate: wake heartbeat now. normal: process on next timer.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Optional event data to include",
                    },
                },
                "required": ["event_type"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "heartbeat_get_instructions": self._handle_get_instructions,
            "heartbeat_set_instructions": self._handle_set_instructions,
            "heartbeat_get_status": self._handle_get_status,
            "heartbeat_pause": self._handle_pause,
            "heartbeat_resume": self._handle_resume,
            "heartbeat_trigger": self._handle_trigger,
            "heartbeat_get_trigger_command": self._handle_get_trigger_command,
        }

    def _get_store(self) -> HeartbeatStore:
        if not hasattr(self, "_store"):
            heartbeat_dir = os.environ.get("HEARTBEAT_DIR", "")
            if not heartbeat_dir:
                raise RuntimeError("HEARTBEAT_DIR not set")
            self._store = HeartbeatStore(Path(heartbeat_dir))
        return self._store

    # -- handlers -----------------------------------------------------------

    def _handle_get_instructions(self, args: dict) -> str:
        store = self._get_store()
        content = store.read_instructions()
        if content is None:
            return (
                "No heartbeat instructions set yet. Use heartbeat_set_instructions to create them."
            )
        return content

    def _handle_set_instructions(self, args: dict) -> str:
        content = args.get("content")
        if content is None:
            return "Error: 'content' is required."
        store = self._get_store()
        nbytes = store.write_instructions(content)
        return f"Heartbeat instructions updated ({nbytes} bytes)."

    def _handle_get_status(self, args: dict) -> str:
        store = self._get_store()
        state = store.load_state()
        lines = [
            f"Enabled: {state.enabled}",
            f"Last run: {state.last_run or 'never'}",
            f"Next run: {state.next_run or 'not scheduled'}",
            f"Total runs: {state.run_count}",
            f"Runs today: {state.runs_today}",
            f"Consecutive idle: {state.consecutive_noop}",
            f"Active session: {state.active_session_id or 'none'}",
        ]
        return "\n".join(lines)

    def _handle_pause(self, args: dict) -> str:
        store = self._get_store()
        store.update_state(enabled=False)
        return "Heartbeat paused. Use heartbeat_resume to re-enable."

    def _handle_resume(self, args: dict) -> str:
        store = self._get_store()
        store.update_state(enabled=True)
        return "Heartbeat resumed."

    def _handle_trigger(self, args: dict) -> str:
        if not self.bridge:
            return "Error: bridge not available"

        event_type = args.get("event_type", "")
        urgency = args.get("urgency", "immediate")
        payload = args.get("payload")

        ctx = self.session_context()
        source = f"session:{ctx.get('id', 'unknown')[:8]}" if ctx else "session"

        try:
            result = self.bridge.call(
                "heartbeat_trigger",
                {
                    "source": source,
                    "event_type": event_type,
                    "urgency": urgency,
                    "payload": payload,
                },
                timeout=5,
            )
        except Exception as e:
            return f"Error: {e}"

        if "error" in result:
            return f"Error: {result['error']}"
        return f"Triggered heartbeat ({urgency}): {event_type}"

    def _handle_get_trigger_command(self, args: dict) -> str:
        socket_path = os.environ.get("CLAUDE_EXT_BRIDGE_SOCKET", "")
        if not socket_path:
            return "Error: bridge socket path not available"

        event_type = args.get("event_type", "")
        if not event_type:
            return "Error: event_type is required"

        urgency = args.get("urgency", "immediate")
        payload = args.get("payload")

        trigger_script = str(Path(__file__).resolve().parent / "trigger_cli.py")

        parts = [
            f"python3 {shlex.quote(trigger_script)}",
            f"--socket {shlex.quote(socket_path)}",
            shlex.quote(event_type),
        ]
        if urgency != "immediate":
            parts.append(f"--urgency {shlex.quote(urgency)}")
        if payload:
            parts.append(f"--payload {shlex.quote(json.dumps(payload))}")

        command = " ".join(parts)

        return (
            f"Trigger command:\n  {command}\n\n"
            f"Usage examples:\n\n"
            f"  # Chain after a long-running command:\n"
            f"  nohup bash -c '<your_command> && {command}' &\n\n"
            f"  # In a monitoring script:\n"
            f"  while true; do\n"
            f"    if <check_condition>; then\n"
            f"      {command}\n"
            f"      break\n"
            f"    fi\n"
            f"    sleep 10\n"
            f"  done"
        )


if __name__ == "__main__":
    HeartbeatMCPServer().run()
