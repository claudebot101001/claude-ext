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
            "name": "heartbeat_instructions",
            "description": (
                "Read or update the heartbeat standing instructions (HEARTBEAT.md). "
                "Omit 'content' to read current instructions. "
                "Provide 'content' to overwrite."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "New instructions content. Omit to read current.",
                    },
                },
            },
        },
        {
            "name": "heartbeat_status",
            "description": (
                "Get heartbeat scheduler status. "
                "Optionally set enabled=false to pause or enabled=true to resume."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "Set to false to pause, true to resume. Omit to just query.",
                    },
                },
            },
        },
        {
            "name": "heartbeat_trigger",
            "description": (
                "Trigger a heartbeat check from within this Claude session (in-process MCP call). "
                "Only works while you are actively executing. "
                "To wake the heartbeat from background shell commands or external scripts, "
                "use heartbeat_get_trigger_command instead."
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
                "Get a standalone shell command that triggers the heartbeat from any external process. "
                "Unlike heartbeat_trigger (which requires an active MCP session), this command can be "
                "run from background jobs, cron scripts, or monitoring loops — even after your session "
                "ends. Use case: 'nohup bash -c \"rsync ... && <command>\" &' to be woken the instant "
                "a long-running transfer finishes, or embed in a price-monitoring script to wake "
                "the heartbeat when a target price is hit."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Event category (e.g. 'transfer_done', 'price_alert')",
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
        {
            "name": "heartbeat_dry_run",
            "description": (
                "Simulate a Tier 2 heartbeat decision without side effects. "
                "Builds the full prompt, calls the LLM, and returns the decision — "
                "but does NOT modify state counters or execute Tier 3. "
                "Useful for testing and refining HEARTBEAT.md instructions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": (
                            "Custom instructions to simulate with. "
                            "Omit to use current HEARTBEAT.md."
                        ),
                    },
                },
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "heartbeat_instructions": self._handle_instructions,
            "heartbeat_status": self._handle_status,
            "heartbeat_trigger": self._handle_trigger,
            "heartbeat_get_trigger_command": self._handle_get_trigger_command,
            "heartbeat_dry_run": self._handle_dry_run,
        }

    def _get_store(self) -> HeartbeatStore:
        if not hasattr(self, "_store"):
            heartbeat_dir = os.environ.get("HEARTBEAT_DIR", "")
            if not heartbeat_dir:
                raise RuntimeError("HEARTBEAT_DIR not set")
            self._store = HeartbeatStore(Path(heartbeat_dir))
        return self._store

    # -- handlers -----------------------------------------------------------

    def _handle_instructions(self, args: dict) -> str:
        content = args.get("content")
        if content is not None:
            store = self._get_store()
            nbytes = store.write_instructions(content)
            return f"Heartbeat instructions updated ({nbytes} bytes)."
        store = self._get_store()
        result = store.read_instructions()
        if result is None:
            return "No heartbeat instructions set yet. Provide 'content' to create them."
        return result

    def _handle_status(self, args: dict) -> str:
        store = self._get_store()
        enabled = args.get("enabled")
        if enabled is not None:
            store.update_state(enabled=bool(enabled))
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
        if enabled is not None:
            action = "resumed" if enabled else "paused"
            hint = "enabled=false" if enabled else "enabled=true"
            lines.append(
                f"\nHeartbeat {action}. "
                f"Use heartbeat_status({hint}) to {'pause' if enabled else 'resume'}."
            )
        return "\n".join(lines)

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

    def _handle_dry_run(self, args: dict) -> str:
        if not self.bridge:
            return "Error: bridge not available"

        params = {}
        instructions = args.get("instructions")
        if instructions is not None:
            params["instructions"] = instructions

        try:
            result = self.bridge.call("heartbeat_dry_run", params, timeout=130)
        except Exception as e:
            return f"Error: {e}"

        if "error" in result:
            return f"Error: {result['error']}"

        lines = [
            f"Decision: {result.get('decision', '')}",
            f"Would execute Tier 3: {result.get('would_execute', False)}",
            f"Noop: {result.get('noop', True)}",
        ]
        prompt = result.get("prompt", "")
        if prompt:
            lines.append(f"\n--- Tier 2 Prompt Used ---\n{prompt}")
        return "\n".join(lines)


if __name__ == "__main__":
    HeartbeatMCPServer().run()
