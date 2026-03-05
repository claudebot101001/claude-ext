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
import dataclasses
import json
import logging
import shlex
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class SessionStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    DEAD = "dead"
    STOPPED = "stopped"


@dataclass
class SessionOverrides:
    """Per-session customizations returned by session customizer callbacks.

    All fields are additive overlays on global registries. None = no opinion.
    """

    extra_system_prompt: list[str] | None = None
    exclude_mcp_servers: set[str] | None = None
    extra_mcp_servers: dict[str, dict] | None = None
    extra_disallowed_tools: list[str] | None = None
    extra_env_unset: list[str] | None = None


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
        now = datetime.now(UTC).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_active_at:
            self.last_active_at = now


# Callback type: (session_id, result_text, metadata)
DeliveryCallback = Callable[[str, str, dict], Awaitable[None]]

# Per-session customizer: receives Session, returns overrides (or None to skip)
SessionCustomizer = Callable[[Session], SessionOverrides | None]


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    STREAM_POLL_INTERVAL = 0.3  # seconds between stream.jsonl reads
    HEARTBEAT_INTERVAL = 30.0  # seconds between "still working" notifications

    def __init__(
        self,
        base_dir: Path,
        engine_config: dict,
        max_sessions_per_user: int = 5,
        session_timeout: float = 7200,
        events=None,
    ):
        self.base_dir = base_dir
        self.engine_config = engine_config
        self.max_sessions_per_user = max_sessions_per_user
        self.session_timeout = session_timeout
        self._events = events  # EventLog instance (optional)
        self.sessions: dict[str, Session] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._monitors: dict[str, asyncio.Task] = {}
        self._delivery_cbs: list[DeliveryCallback] = []
        self._pending_deliveries: list[tuple] = []  # queued until callback is set
        self._mcp_servers: dict[str, dict] = {}  # name -> MCP server config
        self._mcp_tool_meta: dict[str, list[dict]] = {}  # name -> tool metadata
        self._system_prompt_parts: list[tuple[str, str | None]] = []  # (text, mcp_server_name)
        self._env_unset: list[str] = []  # env vars to unset in claude sessions
        self._disallowed_tools: list[str] = []  # built-in tools to disable
        self._session_customizers: list[SessionCustomizer] = []

    def add_delivery_callback(self, cb: DeliveryCallback) -> None:
        """Register a result delivery callback.  Multiple callbacks supported.
        On first registration, flushes any pending deliveries from recover()."""
        self._delivery_cbs.append(cb)
        if self._pending_deliveries:
            for args in self._pending_deliveries:
                asyncio.create_task(cb(*args))
            self._pending_deliveries.clear()

    def register_mcp_server(self, name: str, config: dict, tools: list[dict] | None = None) -> None:
        """Register an MCP server that will be available to all sessions.

        Config follows Claude Code MCP format::

            {"command": "python", "args": ["/path/to/server.py"], "env": {...}}

        Session-specific env vars (CLAUDE_EXT_SESSION_ID, CLAUDE_EXT_STATE_DIR)
        are injected automatically per session.

        The optional *tools* parameter declares the tools this server provides,
        each as ``{"name": ..., "description": ...}``.  Used for introspection
        via :meth:`list_mcp_tools`.
        """
        self._mcp_servers[name] = config
        if tools is not None:
            self._mcp_tool_meta[name] = [
                {"name": t["name"], "description": t.get("description", "")} for t in tools
            ]
        log.info("Registered MCP server: %s (%d tools)", name, len(tools or []))

    def list_mcp_tools(self) -> dict[str, list[dict]]:
        """Return registered MCP servers and their declared tool metadata."""
        return {name: list(self._mcp_tool_meta.get(name, [])) for name in self._mcp_servers}

    def add_system_prompt(self, text: str, mcp_server: str | None = None) -> None:
        """Append a fragment to the system prompt for all sessions.

        Fragments are joined with blank lines and passed to claude via
        ``--append-system-prompt``.  Call during extension ``start()``.

        If *mcp_server* is provided, the fragment is tagged and will be
        excluded from sessions that exclude that MCP server (via
        ``SessionOverrides.exclude_mcp_servers``).  Untagged fragments
        (mcp_server=None) are always included.
        """
        self._system_prompt_parts.append((text, mcp_server))
        tag = f" [tagged: {mcp_server}]" if mcp_server else ""
        log.info("Added system prompt fragment (%d chars)%s", len(text), tag)

    def register_env_unset(self, var_name: str) -> None:
        """Register an environment variable to unset in Claude sessions.

        Extensions call this to prevent sensitive env vars (e.g. passphrases)
        from leaking into the Claude process environment.
        """
        if var_name not in self._env_unset:
            self._env_unset.append(var_name)
            log.info("Registered env var for unset in sessions: %s", var_name)

    def register_disallowed_tool(self, tool_name: str) -> None:
        """Register a built-in tool to disable in Claude sessions.

        Extensions call this when they provide an MCP replacement for a
        built-in tool (e.g. ask_user replaces AskUserQuestion).  The tool
        is passed to ``claude -p`` via ``--disallowedTools``.
        """
        if tool_name not in self._disallowed_tools:
            self._disallowed_tools.append(tool_name)
            log.info("Registered disallowed built-in tool: %s", tool_name)

    def add_session_customizer(self, customizer: SessionCustomizer) -> None:
        """Register a callback to customize per-session configuration.

        Customizers are called in registration order during run script
        generation — i.e. before EVERY prompt execution, not just at
        session creation.  Callbacks must be fast, synchronous, and
        side-effect-free (no I/O, no blocking).

        Each receives the Session object and returns SessionOverrides
        (or None to skip).  Results are merged across all customizers.
        Call during extension start().
        """
        self._session_customizers.append(customizer)

    def _collect_overrides(self, session: Session) -> SessionOverrides:
        """Merge overrides from all registered session customizers."""
        merged = SessionOverrides()
        for customizer in self._session_customizers:
            try:
                result = customizer(session)
            except Exception:
                log.exception("Session customizer %r failed, skipping", customizer)
                continue
            if result is None:
                continue
            # extra_system_prompt
            if result.extra_system_prompt is not None:
                if merged.extra_system_prompt is None:
                    merged.extra_system_prompt = []
                merged.extra_system_prompt.extend(result.extra_system_prompt)
            # exclude_mcp_servers
            if result.exclude_mcp_servers is not None:
                if merged.exclude_mcp_servers is None:
                    merged.exclude_mcp_servers = set()
                merged.exclude_mcp_servers |= result.exclude_mcp_servers
            # extra_mcp_servers
            if result.extra_mcp_servers is not None:
                if merged.extra_mcp_servers is None:
                    merged.extra_mcp_servers = {}
                merged.extra_mcp_servers.update(result.extra_mcp_servers)
            # extra_disallowed_tools
            if result.extra_disallowed_tools is not None:
                if merged.extra_disallowed_tools is None:
                    merged.extra_disallowed_tools = []
                merged.extra_disallowed_tools.extend(result.extra_disallowed_tools)
            # extra_env_unset
            if result.extra_env_unset is not None:
                if merged.extra_env_unset is None:
                    merged.extra_env_unset = []
                merged.extra_env_unset.extend(result.extra_env_unset)
        return merged

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
        raise RuntimeError(f"Session limit ({self.max_sessions_per_user}) reached")

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
            raise RuntimeError(f"Session limit ({self.max_sessions_per_user}) reached")

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
        if self._events:
            self._events.log(
                "session.created", sid, {"slot": slot, "name": session.name, "user_id": user_id}
            )
        log.info("Created session #%d '%s' (%s)", slot, session.name, sid[:8])
        return session

    async def send_prompt(self, session_id: str, prompt: str) -> int:
        """Enqueue a prompt.  Returns queue position (0 = will run next).
        Automatically resets STOPPED sessions.  Rejects DEAD sessions."""
        session = self.sessions[session_id]

        if session.status == SessionStatus.DEAD:
            raise RuntimeError(
                f"Session #{session.slot} '{session.name}' is dead. Delete it and create a new one."
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
        if self._events:
            self._events.log("session.stopped", session_id, {"drained": drained})

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

        if self._events:
            self._events.log(
                "session.destroyed", session_id, {"slot": session.slot, "name": session.name}
            )
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
                    self._pending_deliveries.append(
                        (
                            session.id,
                            result_text,
                            metadata,
                        )
                    )
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
        for fname in (
            "stream.jsonl",
            "output.json",
            "output.json.tmp",
            "stderr.log",
            "exitcode",
            "claude_cmd.sh",
        ):
            (sdir / fname).unlink(missing_ok=True)

        # Write prompt file
        (sdir / "prompt.txt").write_text(prompt, encoding="utf-8")

        is_first = session.prompt_count == 0
        session.prompt_count += 1
        session.last_prompt = prompt[:200]
        session.status = SessionStatus.BUSY
        session.last_active_at = datetime.now(UTC).isoformat()

        if self._events:
            self._events.log("session.prompt", session_id, {"prompt_count": session.prompt_count})

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
        _result_text, metadata = await self._stream_completion(session_id, sdir)

        # If session was stopped/destroyed while we were streaming
        if session.status in (SessionStatus.STOPPED, SessionStatus.DEAD):
            await self.deliver(session_id, "", {"is_stopped": True, "is_final": True})
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

        if self._events:
            self._events.log(
                "session.completed",
                session_id,
                {
                    "cost_usd": metadata.get("total_cost_usd"),
                    "turns": metadata.get("num_turns"),
                },
            )

        # Final delivery — include full result text as fallback in case
        # stream text events were lost (e.g. Telegram send failure during
        # debounce flush).  Frontends should prefer their stream buffer and
        # only use this fallback when the buffer was empty.
        fallback_text, _ = self._parse_stream_result(sdir)
        metadata["is_final"] = True
        await self.deliver(session_id, fallback_text, metadata)

    async def deliver(self, session_id: str, text: str, metadata: dict) -> None:
        """Fan out delivery to all registered callbacks."""
        for cb in self._delivery_cbs:
            try:
                await cb(session_id, text, metadata)
            except Exception:
                log.exception("Delivery callback error")

    def _generate_mcp_config(
        self,
        session: Session,
        sdir: Path,
        *,
        overrides: SessionOverrides | None = None,
    ) -> dict | None:
        """Build per-session MCP config with session-specific env vars.

        Applies *overrides* (if any) following rule R1:
        1. Copy global ``_mcp_servers``
        2. Remove ``exclude_mcp_servers`` entries
        3. Merge ``extra_mcp_servers`` (last-wins)
        """
        # 1. Copy global servers
        servers_to_use = dict(self._mcp_servers)

        # 2. Exclude
        if overrides and overrides.exclude_mcp_servers:
            for name in overrides.exclude_mcp_servers:
                servers_to_use.pop(name, None)

        # 3. Merge extras
        if overrides and overrides.extra_mcp_servers:
            servers_to_use.update(overrides.extra_mcp_servers)

        if not servers_to_use:
            return None

        servers = {}
        for name, cfg in servers_to_use.items():
            entry = dict(cfg)
            env = dict(entry.get("env", {}))
            env["CLAUDE_EXT_SESSION_ID"] = session.id
            env["CLAUDE_EXT_STATE_DIR"] = str(sdir)
            env["CLAUDE_EXT_USER_ID"] = session.user_id
            # Always set bridge socket path — MCP servers connect on demand,
            # not at startup.  The socket will exist by the time they call it.
            env["CLAUDE_EXT_BRIDGE_SOCKET"] = str(self.base_dir / "bridge.sock")
            if self.engine_config.get("gateway_mode"):
                env["CLAUDE_EXT_GATEWAY_MODE"] = "1"
            entry["env"] = env
            servers[name] = entry
        return {"mcpServers": servers}

    def _generate_run_scripts(
        self, session: Session, sdir: Path, is_first: bool
    ) -> tuple[str, str]:
        """Generate the inner claude_cmd.sh and outer run.sh (PTY wrapper).

        Returns (claude_cmd_content, run_sh_content).
        """
        overrides = self._collect_overrides(session)

        prompt_file = shlex.quote(str(sdir / "prompt.txt"))
        stderr_file = shlex.quote(str(sdir / "stderr.log"))
        stream_file = shlex.quote(str(sdir / "stream.jsonl"))
        exitcode_file = shlex.quote(str(sdir / "exitcode"))
        claude_cmd_path = shlex.quote(str(sdir / "claude_cmd.sh"))
        work_dir = shlex.quote(session.working_dir)

        cmd_parts = ["claude", "-p", '"$PROMPT"', "--output-format", "stream-json", "--verbose"]

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
            cmd_parts.extend(["--allowedTools", *list(tools)])

        # Merge config-level and extension-registered disallowed tools
        disallowed = list(self.engine_config.get("disallowed_tools") or [])
        for t in self._disallowed_tools:
            if t not in disallowed:
                disallowed.append(t)
        # Append per-session overrides
        if overrides.extra_disallowed_tools:
            for t in overrides.extra_disallowed_tools:
                if t not in disallowed:
                    disallowed.append(t)
        if disallowed:
            cmd_parts.extend(["--disallowedTools", *disallowed])

        if is_first:
            cmd_parts.extend(["--session-id", session.claude_session_id])
        else:
            cmd_parts.extend(["--resume", session.claude_session_id])

        # MCP config (per-session, includes session-specific env vars + overrides)
        mcp_config = self._generate_mcp_config(session, sdir, overrides=overrides)
        if mcp_config:
            mcp_path = sdir / "mcp_config.json"
            mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
            cmd_parts.extend(["--mcp-config", shlex.quote(str(mcp_path))])

        # System prompt fragments (global + per-session overrides)
        # Filter out tagged prompts whose MCP server is excluded for this session
        excluded = overrides.exclude_mcp_servers or set()
        all_prompt_parts = [
            text
            for text, server in self._system_prompt_parts
            if server is None or server not in excluded
        ]
        if overrides.extra_system_prompt:
            all_prompt_parts.extend(overrides.extra_system_prompt)

        sys_prompt_file = ""
        if all_prompt_parts:
            sys_prompt_path = sdir / "system_prompt.txt"
            sys_prompt_path.write_text(
                "\n\n".join(all_prompt_parts),
                encoding="utf-8",
            )
            sys_prompt_file = shlex.quote(str(sys_prompt_path))

        cmd_str = " ".join(cmd_parts)

        # Build append-system-prompt via file read (same safe pattern as PROMPT)
        sys_prompt_line = ""
        if sys_prompt_file:
            sys_prompt_line = f"SYS_PROMPT=$(cat {sys_prompt_file})\n"
            cmd_str += ' --append-system-prompt "$SYS_PROMPT"'

        # Env unset (global + per-session overrides)
        env_unset_list = ["CLAUDECODE", *self._env_unset]
        if overrides.extra_env_unset:
            env_unset_list.extend(overrides.extra_env_unset)
        unset_vars = " ".join(env_unset_list)

        claude_cmd = (
            "#!/bin/bash\n"
            f"unset {unset_vars}\n"
            f"PROMPT=$(cat {prompt_file})\n"
            f"{sys_prompt_line}"
            f"cd {work_dir}\n"
            f"{cmd_str} 2>{stderr_file}\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            f'script -qfec "bash {claude_cmd_path}" {stream_file}\n'
            f"echo $? > {exitcode_file}\n"
        )

        return claude_cmd, run_sh

    # -- stream event classification -----------------------------------------

    @staticmethod
    def _classify_stream_event(
        event: dict,
    ) -> list[tuple[str, dict]] | None:
        """Classify a stream-json event into deliverable (text, metadata) tuples.

        Returns a list of deliverables, or None for events that should be
        skipped (thinking, tool_result, system, rate_limit_event, etc.).
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
            if event.get("modelUsage"):
                meta["model_usage"] = event["modelUsage"]
            return [("", meta)]

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
                        deliveries.append(
                            (
                                text,
                                {"is_stream": True, "stream_type": "text"},
                            )
                        )
                elif btype == "tool_use":
                    deliveries.append(
                        (
                            "",
                            {
                                "is_stream": True,
                                "stream_type": "tool_use",
                                "tool_name": block.get("name", ""),
                                "tool_input": block.get("input", {}),
                            },
                        )
                    )
                # thinking, tool_result in assistant blocks — skip
            if deliveries:
                # Attach per-turn token usage to the first delivery
                usage = message.get("usage")
                if usage:
                    deliveries[0][1]["usage"] = usage
                return deliveries
            return None

        # Everything else (user, system, rate_limit_event) — skip
        return None

    @staticmethod
    def _iter_stream_events(lines):
        """Yield (text, metadata) for each deliverable stream-json event."""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            classified = SessionManager._classify_stream_event(event)
            if classified is not None:
                yield from classified

    # -- streaming completion -----------------------------------------------

    async def _stream_completion(
        self,
        session_id: str,
        sdir: Path,
        timeout: float | None = None,
    ) -> tuple[str, dict]:
        """Read stream.jsonl incrementally, deliver events in real time.

        Returns ("", final_metadata) on success — text is already delivered
        via streaming callbacks.
        """
        effective_timeout = timeout if timeout is not None else self.session_timeout
        stream_path = sdir / "stream.jsonl"
        exitcode_path = sdir / "exitcode"
        file_pos = 0
        line_buffer = ""
        final_metadata: dict = {}
        start = time.monotonic()
        last_delivery = start

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= effective_timeout:
                return "[Error] Claude Code timed out.", {"is_error": True, "timed_out": True}

            # Incremental read of stream.jsonl
            if stream_path.exists():
                try:
                    with open(stream_path, encoding="utf-8", errors="replace") as f:
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

                    for text, meta in self._iter_stream_events(lines[:-1]):
                        if meta.pop("_is_result", False):
                            final_metadata = meta
                            continue
                        session = self.sessions.get(session_id)
                        if session and session.status == SessionStatus.BUSY:
                            await self.deliver(session_id, text, meta)
                            last_delivery = time.monotonic()

            # Check for completion
            if exitcode_path.exists():
                # Final read to catch any remaining data
                if stream_path.exists():
                    try:
                        with open(stream_path, encoding="utf-8", errors="replace") as f:
                            f.seek(file_pos)
                            remaining = f.read()
                    except OSError:
                        remaining = ""
                    tail = (line_buffer + remaining).strip()
                    if tail:
                        for text, meta in self._iter_stream_events(tail.split("\n")):
                            if meta.pop("_is_result", False):
                                final_metadata = meta
                                continue
                            session = self.sessions.get(session_id)
                            if session and session.status == SessionStatus.BUSY:
                                await self.deliver(session_id, text, meta)

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
                    await self.deliver(
                        session_id,
                        "",
                        {"is_heartbeat": True, "elapsed_s": int(elapsed)},
                    )

            # tmux health check
            session = self.sessions.get(session_id)
            if session and not await self._tmux_has_session(session.tmux_session):
                session.status = SessionStatus.DEAD
                session.error = "tmux session died unexpectedly"
                self._save_state(session)
                if self._events:
                    self._events.log(
                        "session.dead", session_id, {"error": "tmux session died unexpectedly"}
                    )
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
        await self.deliver(session_id, result_text, metadata)

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
            lines = stream_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for text, meta in self._iter_stream_events(lines):
                if meta.pop("_is_result", False):
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
        tmp = sdir / "state.json.tmp"
        tmp.write_text(json.dumps(dataclasses.asdict(session), indent=2), encoding="utf-8")
        tmp.rename(sdir / "state.json")

    def _load_state(self, session_id: str) -> Session | None:
        state_file = self.session_dir(session_id) / "state.json"
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            data["user_id"] = str(data["user_id"])
            data["status"] = SessionStatus(data.get("status", "idle"))
            data.setdefault("slot", 0)
            known = {f.name for f in dataclasses.fields(Session)}
            return Session(**{k: v for k, v in data.items() if k in known})
        except Exception:
            log.exception("Failed to load state for session %s", session_id)
            return None

    # -- tmux helpers -------------------------------------------------------

    async def _tmux_new_session(self, name: str, cwd: str) -> int:
        # Clean up any residual session with the same name (#8)
        await self._tmux_kill_session(name)
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("tmux new-session failed: %s", stderr.decode())
        return proc.returncode

    async def _tmux_send_keys(self, session_name: str, command: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            session_name,
            command,
            "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def _tmux_has_session(self, name: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "has-session",
            "-t",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _tmux_kill_session(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "kill-session",
            "-t",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

    async def _tmux_send_ctrl_c(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            name,
            "C-c",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
