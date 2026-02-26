"""tmux-backed session manager for Claude Code.

Each session runs in its own tmux session, fully decoupled from the main
process.  Communication uses file-based IPC (prompt.txt -> run.sh ->
stream.jsonl -> exitcode) so that a main-process restart never kills a
running claude job.

Output uses ``--output-format stream-json --verbose``, wrapped in
``script -qfec`` to force line-buffered writes via a PTY.  Events are
read incrementally and delivered to callbacks in real time.
"""

import asyncio
import json
import logging
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class SessionStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    DEAD = "dead"
    STOPPED = "stopped"


@dataclass
class Session:
    id: str
    name: str
    slot: int
    user_id: str
    working_dir: str
    context: dict = field(default_factory=dict)
    status: SessionStatus = SessionStatus.IDLE
    claude_session_id: str = ""
    tmux_session: str = ""
    created_at: str = ""
    last_active_at: str = ""
    last_prompt: str = ""
    last_result_metadata: dict = field(default_factory=dict)
    prompt_count: int = 0
    error: str | None = None

    def __post_init__(self):
        if not self.tmux_session:
            self.tmux_session = f"cc-{self.id}"
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_active_at:
            self.last_active_at = now


# Callback type: (session_id, result_text, metadata)
DeliveryCallback = Callable[[str, str, dict], Awaitable[None]]


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    STREAM_POLL_INTERVAL = 0.3  # seconds between stream.jsonl reads
    HEARTBEAT_INTERVAL = 30.0   # seconds between "still working" notifications

    def __init__(
        self,
        base_dir: Path,
        engine_config: dict,
        max_sessions_per_user: int = 5,
    ):
        self.base_dir = base_dir
        self.engine_config = engine_config
        self.max_sessions_per_user = max_sessions_per_user
        self.sessions: dict[str, Session] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._monitors: dict[str, asyncio.Task] = {}
        self._delivery_cbs: list[DeliveryCallback] = []
        self._pending_deliveries: list[tuple] = []  # queued until callback is set
        self._mcp_servers: dict[str, dict] = {}  # name -> MCP server config

    def add_delivery_callback(self, cb: DeliveryCallback) -> None:
        """Register a result delivery callback.  Multiple callbacks supported.
        On first registration, flushes any pending deliveries from recover()."""
        self._delivery_cbs.append(cb)
        if self._pending_deliveries:
            for args in self._pending_deliveries:
                asyncio.create_task(cb(*args))
            self._pending_deliveries.clear()

    def register_mcp_server(self, name: str, config: dict) -> None:
        """Register an MCP server that will be available to all sessions.

        Config follows Claude Code MCP format::

            {"command": "python", "args": ["/path/to/server.py"], "env": {...}}

        Session-specific env vars (CLAUDE_EXT_SESSION_ID, CLAUDE_EXT_STATE_DIR)
        are injected automatically per session.
        """
        self._mcp_servers[name] = config
        log.info("Registered MCP server: %s", name)

    # -- directory helpers --------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        return self.base_dir / "sessions" / session_id

    # -- slot management ----------------------------------------------------

    def _next_slot(self, user_id: str) -> int:
        """Return the lowest unused slot number for the user."""
        used = {s.slot for s in self.sessions.values() if s.user_id == user_id}
        for i in range(1, self.max_sessions_per_user + 1):
            if i not in used:
                return i
        raise RuntimeError(
            f"Session limit ({self.max_sessions_per_user}) reached"
        )

    def get_session_by_slot(self, user_id: str, slot: int) -> Session | None:
        for s in self.sessions.values():
            if s.user_id == user_id and s.slot == slot:
                return s
        return None

    # -- public API ---------------------------------------------------------

    async def create_session(
        self,
        name: str,
        user_id: str,
        working_dir: str,
        context: dict | None = None,
    ) -> Session:
        user_sessions = self.get_sessions_for_user(user_id)
        if len(user_sessions) >= self.max_sessions_per_user:
            raise RuntimeError(
                f"Session limit ({self.max_sessions_per_user}) reached"
            )

        sid = str(uuid.uuid4())
        claude_sid = str(uuid.uuid4())
        slot = self._next_slot(user_id)

        session = Session(
            id=sid,
            name=name,
            slot=slot,
            user_id=user_id,
            working_dir=working_dir,
            context=context or {},
            claude_session_id=claude_sid,
        )

        sdir = self.session_dir(sid)
        sdir.mkdir(parents=True, exist_ok=True)

        rc = await self._tmux_new_session(session.tmux_session, working_dir)
        if rc != 0:
            shutil.rmtree(sdir, ignore_errors=True)
            raise RuntimeError(f"Failed to create tmux session: {session.tmux_session}")

        self.sessions[sid] = session
        self._setup_queue(sid)
        self._save_state(session)
        log.info("Created session #%d '%s' (%s)", slot, session.name, sid[:8])
        return session

    async def send_prompt(self, session_id: str, prompt: str) -> int:
        """Enqueue a prompt.  Returns queue position (0 = will run next).
        Automatically resets STOPPED sessions.  Rejects DEAD sessions."""
        session = self.sessions[session_id]

        if session.status == SessionStatus.DEAD:
            raise RuntimeError(
                f"Session #{session.slot} '{session.name}' is dead. "
                f"Delete it and create a new one."
            )

        # Reset stopped sessions on new input
        if session.status == SessionStatus.STOPPED:
            session.status = SessionStatus.IDLE
            session.error = None
            self._save_state(session)

        queue = self._queues[session_id]
        position = queue.qsize()
        if session.status == SessionStatus.BUSY:
            position += 1
        await queue.put(prompt)
        return position

    def get_sessions_for_user(self, user_id: str) -> list[Session]:
        return sorted(
            (s for s in self.sessions.values() if s.user_id == user_id),
            key=lambda s: s.slot,
        )

    def get_session_by_name(self, user_id: str, name: str) -> Session | None:
        for s in self.sessions.values():
            if s.user_id == user_id and s.name == name:
                return s
        return None

    async def stop_session(self, session_id: str) -> tuple[bool, int]:
        """Stop running task + drain queue.  Non-blocking.
        Returns (stopped_running_task, drained_queue_count)."""
        session = self.sessions.get(session_id)
        if not session:
            return False, 0

        # Drain queue regardless of status
        drained = 0
        queue = self._queues.get(session_id)
        if queue:
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                    drained += 1
                except asyncio.QueueEmpty:
                    break

        if session.status != SessionStatus.BUSY:
            return False, drained

        # Set STOPPED *before* Ctrl-C to prevent _execute_prompt race
        session.status = SessionStatus.STOPPED
        self._save_state(session)

        # Send Ctrl-C (non-blocking)
        await self._tmux_send_ctrl_c(session.tmux_session)

        # Background: ensure exitcode exists after delay so poller unblocks
        asyncio.create_task(self._ensure_stopped(session_id))

        return True, drained

    async def _ensure_stopped(self, session_id: str) -> None:
        """Background task: write synthetic exitcode if claude didn't exit."""
        await asyncio.sleep(5)
        sdir = self.session_dir(session_id)
        if sdir.exists() and not (sdir / "exitcode").exists():
            (sdir / "exitcode").write_text("130")

    async def destroy_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        worker = self._workers.pop(session_id, None)
        if worker:
            worker.cancel()
        monitor = self._monitors.pop(session_id, None)
        if monitor:
            monitor.cancel()
        self._queues.pop(session_id, None)

        if await self._tmux_has_session(session.tmux_session):
            await self._tmux_kill_session(session.tmux_session)

        sdir = self.session_dir(session_id)
        if sdir.exists():
            shutil.rmtree(sdir)

        log.info("Destroyed session #%d '%s' (%s)", session.slot, session.name, session_id[:8])

    async def shutdown(self) -> None:
        """Cancel workers/monitors but keep tmux sessions alive."""
        for task in list(self._workers.values()) + list(self._monitors.values()):
            task.cancel()
        self._workers.clear()
        self._monitors.clear()
        log.info("SessionManager shut down.  tmux sessions preserved.")

    # -- recovery -----------------------------------------------------------

    async def recover(self) -> None:
        """On startup: reload persisted state and reconnect to tmux.
        Pending result deliveries are buffered until add_delivery_callback()."""
        sessions_dir = self.base_dir / "sessions"
        if not sessions_dir.exists():
            return

        for child in sorted(sessions_dir.iterdir()):
            if not child.is_dir():
                continue
            session = self._load_state(child.name)
            if session is None:
                continue

            tmux_alive = await self._tmux_has_session(session.tmux_session)
            exitcode_exists = (child / "exitcode").exists()

            if session.status == SessionStatus.BUSY:
                if tmux_alive and exitcode_exists:
                    # Finished while we were down — buffer result for delivery
                    result_text, metadata = self._parse_stream_result(child)
                    metadata["is_final"] = True
                    session.status = SessionStatus.IDLE
                    session.last_result_metadata = metadata
                    self._save_state(session)
                    self.sessions[session.id] = session
                    self._setup_queue(session.id)
                    self._pending_deliveries.append((
                        session.id, result_text, metadata,
                    ))
                    log.info("Recovered completed session %s", session.name)

                elif tmux_alive and not exitcode_exists:
                    # Still running — resume monitoring
                    self.sessions[session.id] = session
                    self._setup_queue(session.id)
                    self._monitors[session.id] = asyncio.create_task(
                        self._resume_monitor(session.id, child),
                        name=f"monitor-{session.id[:8]}",
                    )
                    log.info("Resumed monitoring for session %s", session.name)

                else:
                    # tmux gone
                    session.status = SessionStatus.DEAD
                    session.error = "tmux session lost during restart"
                    self._save_state(session)
                    self.sessions[session.id] = session
                    self._setup_queue(session.id)
                    log.warning("Session %s marked dead (tmux gone)", session.name)

            elif session.status in (SessionStatus.IDLE, SessionStatus.STOPPED):
                if tmux_alive:
                    self.sessions[session.id] = session
                    self._setup_queue(session.id)
                    log.info("Reconnected to session %s", session.name)
                else:
                    rc = await self._tmux_new_session(session.tmux_session, session.working_dir)
                    if rc == 0:
                        if session.status == SessionStatus.STOPPED:
                            session.status = SessionStatus.IDLE
                            self._save_state(session)
                        self.sessions[session.id] = session
                        self._setup_queue(session.id)
                        log.info("Recreated tmux for session %s", session.name)
                    else:
                        session.status = SessionStatus.DEAD
                        session.error = "Failed to recreate tmux session"
                        self._save_state(session)
                        self.sessions[session.id] = session
                        self._setup_queue(session.id)

            elif session.status == SessionStatus.DEAD:
                # Load dead sessions so users can see and /delete them
                self.sessions[session.id] = session
                self._setup_queue(session.id)
                log.info("Recovered dead session #%d '%s'", session.slot, session.name)

    # -- internal: queue & execution ----------------------------------------

    def _setup_queue(self, session_id: str) -> None:
        self._queues[session_id] = asyncio.Queue()
        self._workers[session_id] = asyncio.create_task(
            self._queue_worker(session_id),
            name=f"worker-{session_id[:8]}",
        )

    async def _queue_worker(self, session_id: str) -> None:
        queue = self._queues[session_id]
        try:
            while True:
                prompt = await queue.get()
                try:
                    session = self.sessions.get(session_id)
                    # Skip if session was stopped/dead/destroyed while queued
                    if not session or session.status in (SessionStatus.STOPPED, SessionStatus.DEAD):
                        continue
                    await self._execute_prompt(session_id, prompt)
                except Exception:
                    log.exception("Error executing prompt in session %s", session_id[:8])
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            return

    async def _execute_prompt(self, session_id: str, prompt: str) -> None:
        session = self.sessions[session_id]
        sdir = self.session_dir(session_id)

        # Clean previous artifacts
        for fname in ("stream.jsonl", "output.json", "output.json.tmp",
                       "stderr.log", "exitcode", "claude_cmd.sh"):
            (sdir / fname).unlink(missing_ok=True)

        # Write prompt file
        (sdir / "prompt.txt").write_text(prompt, encoding="utf-8")

        is_first = session.prompt_count == 0
        session.prompt_count += 1
        session.last_prompt = prompt[:200]
        session.status = SessionStatus.BUSY
        session.last_active_at = datetime.now(timezone.utc).isoformat()

        # Generate and write run scripts (inner + outer)
        claude_cmd, run_sh = self._generate_run_scripts(session, sdir, is_first)
        (sdir / "claude_cmd.sh").write_text(claude_cmd, encoding="utf-8")
        (sdir / "run.sh").write_text(run_sh, encoding="utf-8")
        self._save_state(session)

        # Execute in tmux
        await self._tmux_send_keys(
            session.tmux_session,
            f"bash {shlex.quote(str(sdir / 'run.sh'))}",
        )

        # Stream completion (delivers events in real time)
        result_text, metadata = await self._stream_completion(session_id, sdir)

        # If session was stopped/destroyed while we were streaming
        if session.status in (SessionStatus.STOPPED, SessionStatus.DEAD):
            await self._deliver(session_id, "", {"is_stopped": True, "is_final": True})
            return
        if session_id not in self.sessions:
            return

        # Timeout — try to terminate the orphaned process
        if metadata.get("is_error") and metadata.get("timed_out"):
            await self._tmux_send_ctrl_c(session.tmux_session)

        # Update state
        session.status = SessionStatus.IDLE
        session.last_result_metadata = metadata
        if metadata.get("claude_session_id"):
            session.claude_session_id = metadata["claude_session_id"]
        session.error = None
        self._save_state(session)

        # Final delivery (text already streamed; metadata-only)
        metadata["is_final"] = True
        await self._deliver(session_id, "", metadata)

    async def _deliver(self, session_id: str, text: str, metadata: dict) -> None:
        """Fan out delivery to all registered callbacks."""
        for cb in self._delivery_cbs:
            try:
                await cb(session_id, text, metadata)
            except Exception:
                log.exception("Delivery callback error")

    def _generate_mcp_config(self, session: Session, sdir: Path) -> dict | None:
        """Build per-session MCP config with session-specific env vars."""
        if not self._mcp_servers:
            return None
        servers = {}
        for name, cfg in self._mcp_servers.items():
            entry = dict(cfg)
            env = dict(entry.get("env", {}))
            env["CLAUDE_EXT_SESSION_ID"] = session.id
            env["CLAUDE_EXT_STATE_DIR"] = str(sdir)
            entry["env"] = env
            servers[name] = entry
        return {"mcpServers": servers}

    def _generate_run_scripts(self, session: Session, sdir: Path, is_first: bool) -> tuple[str, str]:
        """Generate the inner claude_cmd.sh and outer run.sh (PTY wrapper).

        Returns (claude_cmd_content, run_sh_content).
        """
        prompt_file = shlex.quote(str(sdir / "prompt.txt"))
        stderr_file = shlex.quote(str(sdir / "stderr.log"))
        stream_file = shlex.quote(str(sdir / "stream.jsonl"))
        exitcode_file = shlex.quote(str(sdir / "exitcode"))
        claude_cmd_path = shlex.quote(str(sdir / "claude_cmd.sh"))
        work_dir = shlex.quote(session.working_dir)

        cmd_parts = ["claude", "-p", '"$PROMPT"',
                      "--output-format", "stream-json", "--verbose"]

        model = self.engine_config.get("model")
        if model:
            cmd_parts.extend(["--model", model])
        max_turns = self.engine_config.get("max_turns", 0)
        if max_turns > 0:
            cmd_parts.extend(["--max-turns", str(max_turns)])
        perm = self.engine_config.get("permission_mode", "bypassPermissions")
        if perm:
            cmd_parts.extend(["--permission-mode", perm])
        tools = self.engine_config.get("allowed_tools")
        if tools:
            cmd_parts.extend(["--allowedTools"] + list(tools))

        if is_first:
            cmd_parts.extend(["--session-id", session.claude_session_id])
        else:
            cmd_parts.extend(["--resume", session.claude_session_id])

        # MCP config (per-session, includes session-specific env vars)
        mcp_config = self._generate_mcp_config(session, sdir)
        if mcp_config:
            mcp_path = sdir / "mcp_config.json"
            mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
            cmd_parts.extend(["--mcp-config", shlex.quote(str(mcp_path))])

        cmd_str = " ".join(cmd_parts)

        claude_cmd = (
            "#!/bin/bash\n"
            "unset CLAUDECODE\n"
            f"PROMPT=$(cat {prompt_file})\n"
            f"cd {work_dir}\n"
            f"{cmd_str} 2>{stderr_file}\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            f"script -qfec \"bash {claude_cmd_path}\" {stream_file}\n"
            f"echo $? > {exitcode_file}\n"
        )

        return claude_cmd, run_sh

    # -- stream event classification -----------------------------------------

    @staticmethod
    def _classify_stream_event(event: dict) -> tuple[str, dict] | None:
        """Classify a stream-json event into a deliverable (text, metadata) or None.

        Returns None for events that should be skipped (thinking, tool_result,
        system, rate_limit_event, etc.).
        """
        etype = event.get("type")

        # Final result event — extract metadata, don't deliver text
        if etype == "result":
            meta = {
                "claude_session_id": event.get("session_id"),
                "total_cost_usd": event.get("total_cost_usd"),
                "duration_ms": event.get("duration_ms"),
                "duration_api_ms": event.get("duration_api_ms"),
                "num_turns": event.get("num_turns"),
                "is_error": event.get("is_error", False),
                "model": event.get("model"),
                "_is_result": True,  # internal flag
            }
            return "", meta

        # Assistant message — inspect content blocks
        if etype == "assistant":
            message = event.get("message", {})
            content = message.get("content", [])
            deliveries = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        deliveries.append((
                            text,
                            {"is_stream": True, "stream_type": "text"},
                        ))
                elif btype == "tool_use":
                    deliveries.append((
                        "",
                        {
                            "is_stream": True,
                            "stream_type": "tool_use",
                            "tool_name": block.get("name", ""),
                            "tool_input": block.get("input", {}),
                        },
                    ))
                # thinking, tool_result in assistant blocks — skip
            if deliveries:
                # Return only the first delivery; caller will get others
                # by re-parsing.  In practice assistant events have one block.
                return deliveries[0]
            return None

        # Everything else (user, system, rate_limit_event) — skip
        return None

    # -- streaming completion -----------------------------------------------

    async def _stream_completion(
        self, session_id: str, sdir: Path, timeout: float = 600,
    ) -> tuple[str, dict]:
        """Read stream.jsonl incrementally, deliver events in real time.

        Returns ("", final_metadata) on success — text is already delivered
        via streaming callbacks.
        """
        stream_path = sdir / "stream.jsonl"
        exitcode_path = sdir / "exitcode"
        file_pos = 0
        line_buffer = ""
        final_metadata: dict = {}
        start = time.monotonic()
        last_delivery = start

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return "[Error] Claude Code timed out.", {"is_error": True, "timed_out": True}

            # Incremental read of stream.jsonl
            if stream_path.exists():
                try:
                    with open(stream_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(file_pos)
                        new_data = f.read()
                        file_pos = f.tell()
                except OSError:
                    new_data = ""

                if new_data:
                    line_buffer += new_data
                    lines = line_buffer.split("\n")
                    # Last element is either "" (complete line) or partial
                    line_buffer = lines[-1]

                    for line in lines[:-1]:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue  # script header or malformed line

                        classified = self._classify_stream_event(event)
                        if classified is None:
                            continue

                        text, meta = classified
                        if meta.get("_is_result"):
                            meta.pop("_is_result", None)
                            final_metadata = meta
                            continue

                        # Deliver stream event
                        session = self.sessions.get(session_id)
                        if session and session.status == SessionStatus.BUSY:
                            await self._deliver(session_id, text, meta)
                            last_delivery = time.monotonic()

            # Check for completion
            if exitcode_path.exists():
                # Final read to catch any remaining data
                if stream_path.exists():
                    try:
                        with open(stream_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(file_pos)
                            remaining = f.read()
                    except OSError:
                        remaining = ""
                    if remaining:
                        tail = (line_buffer + remaining).strip()
                        for line in tail.split("\n"):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            classified = self._classify_stream_event(event)
                            if classified is None:
                                continue
                            text, meta = classified
                            if meta.get("_is_result"):
                                meta.pop("_is_result", None)
                                final_metadata = meta
                                continue
                            session = self.sessions.get(session_id)
                            if session and session.status == SessionStatus.BUSY:
                                await self._deliver(session_id, text, meta)

                # If no result event was found, fall back to old parser
                if not final_metadata:
                    _, fallback = self._parse_result(sdir)
                    final_metadata = fallback

                return "", final_metadata

            await asyncio.sleep(self.STREAM_POLL_INTERVAL)

            # Heartbeat (only if no recent delivery)
            now_mono = time.monotonic()
            if (now_mono - last_delivery) >= self.HEARTBEAT_INTERVAL:
                last_delivery = now_mono
                session = self.sessions.get(session_id)
                if session and session.status == SessionStatus.BUSY and self._delivery_cbs:
                    await self._deliver(
                        session_id, "",
                        {"is_heartbeat": True, "elapsed_s": int(elapsed)},
                    )

            # tmux health check
            session = self.sessions.get(session_id)
            if session and not await self._tmux_has_session(session.tmux_session):
                session.status = SessionStatus.DEAD
                session.error = "tmux session died unexpectedly"
                self._save_state(session)
                return "[Error] tmux session died unexpectedly.", {"is_error": True}

    async def _resume_monitor(self, session_id: str, sdir: Path) -> None:
        """Resume monitoring a session that was running before restart."""
        result_text, metadata = await self._stream_completion(session_id, sdir)
        session = self.sessions.get(session_id)
        if not session or session.status in (SessionStatus.STOPPED, SessionStatus.DEAD):
            return
        session.status = SessionStatus.IDLE
        session.last_result_metadata = metadata
        if metadata.get("claude_session_id"):
            session.claude_session_id = metadata["claude_session_id"]
        self._save_state(session)
        metadata["is_final"] = True
        await self._deliver(session_id, result_text, metadata)

    # -- result parsing -----------------------------------------------------

    def _parse_stream_result(self, sdir: Path) -> tuple[str, dict]:
        """Parse stream.jsonl to extract full text + final metadata.

        Used during recovery when events were not streamed in real time.
        """
        stream_path = sdir / "stream.jsonl"
        if not stream_path.exists() or stream_path.stat().st_size == 0:
            # Fall back to legacy output.json
            return self._parse_result(sdir)

        text_parts: list[str] = []
        final_meta: dict = {}

        try:
            for line in stream_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                classified = self._classify_stream_event(event)
                if classified is None:
                    continue
                text, meta = classified
                if meta.get("_is_result"):
                    meta.pop("_is_result", None)
                    final_meta = meta
                elif meta.get("stream_type") == "text" and text:
                    text_parts.append(text)
        except OSError:
            return self._parse_result(sdir)

        full_text = "".join(text_parts)
        if not final_meta and not full_text:
            return self._parse_result(sdir)
        return full_text, final_meta

    def _parse_result(self, sdir: Path) -> tuple[str, dict]:
        output_path = sdir / "output.json"

        if not output_path.exists() or output_path.stat().st_size == 0:
            stderr = ""
            stderr_path = sdir / "stderr.log"
            if stderr_path.exists():
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            return f"[Error] No output from Claude Code. {stderr}", {"is_error": True}

        try:
            data = json.loads(output_path.read_text(encoding="utf-8"))
            metadata = {
                "claude_session_id": data.get("session_id"),
                "total_cost_usd": data.get("total_cost_usd"),
                "duration_ms": data.get("duration_ms"),
                "duration_api_ms": data.get("duration_api_ms"),
                "num_turns": data.get("num_turns"),
                "is_error": data.get("is_error", False),
                "model": data.get("model"),
            }
            return data.get("result", ""), metadata
        except (json.JSONDecodeError, TypeError):
            raw = output_path.read_text(encoding="utf-8", errors="replace")
            return raw, {"is_error": True, "parse_error": True}

    # -- state persistence --------------------------------------------------

    def _save_state(self, session: Session) -> None:
        sdir = self.session_dir(session.id)
        sdir.mkdir(parents=True, exist_ok=True)
        state = {
            "id": session.id,
            "name": session.name,
            "slot": session.slot,
            "user_id": session.user_id,
            "context": session.context,
            "working_dir": session.working_dir,
            "status": session.status.value,
            "claude_session_id": session.claude_session_id,
            "tmux_session": session.tmux_session,
            "created_at": session.created_at,
            "last_active_at": session.last_active_at,
            "last_prompt": session.last_prompt,
            "last_result_metadata": session.last_result_metadata,
            "prompt_count": session.prompt_count,
            "error": session.error,
        }
        tmp = sdir / "state.json.tmp"
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.rename(sdir / "state.json")

    def _load_state(self, session_id: str) -> Session | None:
        sdir = self.session_dir(session_id)
        state_file = sdir / "state.json"
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            # Backward compat: old format has chat_id at top level, no context
            context = data.get("context", {})
            if not context and "chat_id" in data:
                context = {"chat_id": data["chat_id"]}
            # Backward compat: old format has user_id as int
            user_id = str(data["user_id"])
            return Session(
                id=data["id"],
                name=data["name"],
                slot=data.get("slot", 0),
                user_id=user_id,
                working_dir=data["working_dir"],
                context=context,
                status=SessionStatus(data.get("status", "idle")),
                claude_session_id=data.get("claude_session_id", ""),
                tmux_session=data.get("tmux_session", f"cc-{data['id']}"),
                created_at=data.get("created_at", ""),
                last_active_at=data.get("last_active_at", ""),
                last_prompt=data.get("last_prompt", ""),
                last_result_metadata=data.get("last_result_metadata", {}),
                prompt_count=data.get("prompt_count", 0),
                error=data.get("error"),
            )
        except Exception:
            log.exception("Failed to load state for session %s", session_id)
            return None

    # -- tmux helpers -------------------------------------------------------

    async def _tmux_new_session(self, name: str, cwd: str) -> int:
        # Clean up any residual session with the same name (#8)
        await self._tmux_kill_session(name)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", name, "-c", cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("tmux new-session failed: %s", stderr.decode())
        return proc.returncode

    async def _tmux_send_keys(self, session_name: str, command: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, command, "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def _tmux_has_session(self, name: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _tmux_kill_session(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

    async def _tmux_send_ctrl_c(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", name, "C-c",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
