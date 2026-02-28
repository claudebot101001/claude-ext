"""Claude Code engine - thin wrapper around the claude CLI."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from core.bridge import BridgeServer
from core.events import EventLog
from core.pending import PendingStore
from core.session import SessionManager

log = logging.getLogger(__name__)


class ClaudeEngine:
    """Wraps `claude` CLI. Tracks last session metadata."""

    def __init__(
        self,
        model: str | None = None,
        max_turns: int = 0,
        permission_mode: str = "bypassPermissions",
        allowed_tools: list[str] | None = None,
    ):
        self.model = model
        self.max_turns = max_turns  # 0 = unlimited
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.last_session: dict = {}  # metadata from last JSON response
        self.session_manager: SessionManager | None = None
        self.bridge: BridgeServer | None = None
        self.pending = PendingStore()
        self.events: EventLog | None = None
        self.registry = None  # set by main.py after Registry is created
        self.services: dict[str, Any] = {}

    def init_sessions(self, base_dir: Path, max_sessions_per_user: int = 5) -> None:
        """Initialize the tmux-backed session manager."""
        self.events = EventLog(base_dir / "events.jsonl")
        self.session_manager = SessionManager(
            base_dir=base_dir,
            engine_config={
                "model": self.model,
                "max_turns": self.max_turns,
                "permission_mode": self.permission_mode,
                "allowed_tools": self.allowed_tools,
            },
            max_sessions_per_user=max_sessions_per_user,
            events=self.events,
        )
        self.bridge = BridgeServer(base_dir / "bridge.sock")

    def _build_cmd(self, prompt: str, continue_session: bool = False) -> list[str]:
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.max_turns > 0:
            cmd.extend(["--max-turns", str(self.max_turns)])
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        if self.allowed_tools:
            cmd.extend(["--allowedTools", *self.allowed_tools])
        if continue_session:
            cmd.append("--continue")
        return cmd

    async def ask(
        self,
        prompt: str,
        cwd: str | None = None,
        continue_session: bool = False,
        timeout: float = 300,
    ) -> str:
        """Send a prompt to Claude Code and return the text response."""
        cmd = self._build_cmd(prompt, continue_session)
        log.info("Running: %s", " ".join(cmd[:6]))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "[Error] Claude Code timed out."

        raw = stdout.decode().strip()

        if proc.returncode != 0:
            err = stderr.decode().strip()
            log.error("Claude Code error (rc=%d): %s", proc.returncode, err)
            return f"[Error] Claude Code failed: {err}"

        # Parse JSON response, extract text result and store metadata
        try:
            data = json.loads(raw)
            self.last_session = {
                "session_id": data.get("session_id"),
                "total_cost_usd": data.get("total_cost_usd"),
                "duration_ms": data.get("duration_ms"),
                "duration_api_ms": data.get("duration_api_ms"),
                "num_turns": data.get("num_turns"),
                "is_error": data.get("is_error", False),
                "model": data.get("model"),
            }
            return data.get("result", raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("Failed to parse JSON response, returning raw output")
            return raw
