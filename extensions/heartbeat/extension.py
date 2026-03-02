"""Heartbeat extension — autonomous periodic task execution.

Dual-channel scheduler (timer + event triggers) with three-tier execution:
  Tier 0: Python gate checks (zero cost)
  Tier 1: Pre-checks (instructions exist? events pending?)
  Tier 2: LLM decision via engine.ask() (low cost, ~500 input tokens)
  Tier 3: Full session execution (normal cost, only when action needed)
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.extension import Extension
from core.session import SessionStatus
from core.status import get_usage
from extensions.heartbeat.store import HeartbeatState, HeartbeatStore

log = logging.getLogger(__name__)

# Adaptive backoff table: (max_consecutive_noop, multiplier)
_BACKOFF_TABLE = [
    (3, 1),
    (6, 2),
    (9, 4),
]
_BACKOFF_MAX_MULTIPLIER = 8

_SEED_INSTRUCTIONS = """\
# Heartbeat Instructions

This file defines what the autonomous heartbeat agent checks periodically.

## Standing Tasks
<!-- Add tasks to check on each cycle -->
<!-- Example: Check deployment status -->

## Conditions for Action
<!-- When to act vs. stay silent -->

## Notes
<!-- Agent reads this on each cycle and decides if action is needed -->
<!-- If nothing needs attention, it stays silent (no notification) -->
"""

_SYSTEM_PROMPT = """\
You have an autonomous heartbeat that periodically checks standing tasks. \
Manage via MCP: heartbeat_instructions (read/write HEARTBEAT.md), \
heartbeat_status (view scheduler state; set enabled=false to pause, true to resume). \
Two ways to trigger the heartbeat on-demand: \
heartbeat_trigger (in-session MCP call — works only while you are running), \
heartbeat_get_trigger_command (returns a standalone shell command for external/background processes \
where MCP tools are unavailable — e.g., 'nohup bash -c "rsync ... && <trigger_cmd>" &' \
to be woken the instant a background task completes). \
When asked to monitor something periodically, consider adding to heartbeat instructions."""

_TIER2_PROMPT_TEMPLATE = """\
You are an autonomous heartbeat agent. Your job is to decide whether any action is needed right now.

## Standing Instructions
{instructions}

{context_section}
Review the above and decide:
- If nothing needs attention, reply with: NOTHING
- If action is needed, reply with a brief task description (1-2 sentences). Do NOT execute the task, just describe it.

Your ENTIRE response must be the single word NOTHING, or a 1-2 sentence task description. No preamble, no analysis, no explanation."""

_TIER3_PROMPT_TEMPLATE = """\
[HEARTBEAT #{run_count} — {timestamp}]

## Task
{decision}

## Standing Instructions
{instructions}

{trigger_context}
Execute this task now. Use all available tools. Summarize results when done."""

# Usage cache TTL
_NOOP_MAX_LEN = 200

_USAGE_CACHE_TTL = 60.0

# Cleanup delay after Tier 3 completion
_CLEANUP_DELAY = 5.0


@dataclass
class TriggerEvent:
    source: str  # Source extension name
    event_type: str  # Event category
    urgency: str  # "immediate" | "normal"
    payload: dict  # Event data
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class ExtensionImpl(Extension):
    name = "heartbeat"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._interval = config.get("interval", 1800)
        self._active_hours = config.get("active_hours")
        self._max_daily_runs = config.get("max_daily_runs", 48)
        self._usage_throttle = config.get("usage_throttle", 80)
        self._usage_pause = config.get("usage_pause", 95)
        self._user_id = str(config.get("user_id", ""))
        self._notify_context = config.get("notify_context", {})
        self._working_dir = config.get("working_dir") or os.getcwd()

        self._restart_marker: Path | None = None  # set in start()
        self._store: HeartbeatStore | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._trigger_queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self._pending_events: list[TriggerEvent] = []
        self._usage_cache: dict | None = None
        self._usage_cache_ts: float = 0.0

    def reconfigure(self, config: dict) -> None:
        super().reconfigure(config)
        self._interval = config.get("interval", self._interval)
        self._max_daily_runs = config.get("max_daily_runs", self._max_daily_runs)
        self._usage_throttle = config.get("usage_throttle", self._usage_throttle)
        self._usage_pause = config.get("usage_pause", self._usage_pause)
        log.info(
            "Heartbeat config reloaded: interval=%d, max_daily=%d",
            self._interval,
            self._max_daily_runs,
        )

    @property
    def sm(self):
        return self.engine.session_manager

    # -- public API (called by other extensions) -----------------------------

    def trigger(
        self, source: str, event_type: str, urgency: str, payload: dict | None = None
    ) -> None:
        """Submit a trigger event. Sync method (put_nowait / append).

        urgency="immediate": wakes scheduler immediately.
        urgency="normal": accumulated until next timer check.
        """
        event = TriggerEvent(
            source=source,
            event_type=event_type,
            urgency=urgency,
            payload=payload or {},
        )
        if urgency == "immediate":
            self._trigger_queue.put_nowait(event)
            if self.engine.events:
                self.engine.events.log(
                    "heartbeat.triggered",
                    detail={
                        "source": source,
                        "event_type": event_type,
                    },
                )
        else:
            self._pending_events.append(event)

    def drain_pending_events(self) -> list[TriggerEvent]:
        """Drain and return accumulated normal-urgency events."""
        events = self._pending_events[:]
        self._pending_events.clear()
        return events

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if not self._user_id:
            raise RuntimeError("heartbeat: 'user_id' is required in config")

        # 1. Initialize store
        heartbeat_dir = Path(self.sm.base_dir) / "heartbeat"
        self._store = HeartbeatStore(heartbeat_dir)

        # 2. Recovery: clear stale active_session_id
        state = self._store.load_state()
        if state.active_session_id:
            session = self.sm.sessions.get(state.active_session_id)
            if not session or session.status in (
                SessionStatus.DEAD,
                SessionStatus.STOPPED,
                SessionStatus.IDLE,
            ):
                log.warning(
                    "Cleared stale active_session_id: %s", (state.active_session_id or "")[:8]
                )
                self._store.update_state(active_session_id=None)

        # 3. Restart marker safety net
        self._restart_marker = heartbeat_dir / ".restart_marker"
        state = self._store.load_state()  # re-read after recovery cleanup
        if state.pending_verification:
            if self._restart_marker.exists():
                # Double restart: previous restart didn't reach verification.
                # The committed code likely broke something — auto-revert.
                commit = state.pending_verification
                log.warning("Double restart with pending verification %s, auto-reverting", commit)
                check = subprocess.run(
                    ["git", "rev-parse", "--verify", commit],
                    cwd=self._working_dir,
                    capture_output=True,
                )
                if check.returncode == 0:
                    revert = subprocess.run(
                        ["git", "revert", commit, "--no-edit"],
                        cwd=self._working_dir,
                        capture_output=True,
                    )
                    if revert.returncode == 0:
                        log.warning("Auto-reverted commit %s", commit)
                    else:
                        stderr_text = revert.stderr.decode(errors="replace")
                        log.error("git revert failed: %s", stderr_text[:500])
                        self._handle_revert_failure(commit, stderr_text)
                else:
                    log.warning("Commit %s not found, clearing stale verification", commit)
                self._store.update_state(pending_verification=None)
                self._restart_marker.unlink(missing_ok=True)
            else:
                # First restart after commit — write marker for double-restart detection
                self._restart_marker.write_text(state.pending_verification)
                log.info(
                    "Restart marker written for verification of %s", state.pending_verification
                )
                # Fast-schedule: bypass backoff so verification runs quickly
                near_future = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
                self._store.update_state(next_run=near_future, consecutive_noop=0)
                log.info("Pending verification detected, scheduling fast check in 30s")

        # 4. Register service (self, not store — includes trigger())
        self.engine.services["heartbeat"] = self

        # 5. Register MCP server
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        self.sm.register_mcp_server(
            "heartbeat",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {"HEARTBEAT_DIR": str(heartbeat_dir)},
            },
            tools=[
                {
                    "name": "heartbeat_instructions",
                    "description": "Read or update heartbeat standing instructions",
                },
                {
                    "name": "heartbeat_status",
                    "description": "Get heartbeat status; optionally pause/resume",
                },
                {
                    "name": "heartbeat_trigger",
                    "description": "Trigger heartbeat from within this session (in-process call)",
                },
                {
                    "name": "heartbeat_get_trigger_command",
                    "description": "Get standalone shell command to trigger heartbeat from external/background processes",
                },
                {
                    "name": "heartbeat_dry_run",
                    "description": "Simulate a Tier 2 decision without side effects (for testing instructions)",
                },
                {
                    "name": "heartbeat_set_verification",
                    "description": "Record a commit hash for post-restart verification, or null to clear",
                },
            ],
        )

        # 6. Bridge handler (MCP → trigger)
        if self.engine.bridge:
            self.engine.bridge.add_handler(self._handle_bridge_request)

        # 7. System prompt
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="heartbeat")

        # 8. Delivery callback
        self.sm.add_delivery_callback(self._on_delivery)

        # 8b. Re-schedule cleanup for orphaned heartbeat sessions
        for sid, session in list(self.sm.sessions.items()):
            if session.context.get("heartbeat_auto_cleanup") and session.status in (
                SessionStatus.IDLE,
                SessionStatus.STOPPED,
            ):
                asyncio.create_task(self._delayed_cleanup(sid))
                log.info("Re-scheduled cleanup for orphaned session %s", sid[:8])

        # 9. Seed HEARTBEAT.md
        if not (heartbeat_dir / "HEARTBEAT.md").exists():
            self._store.write_instructions(_SEED_INSTRUCTIONS)
            log.info("Created seed HEARTBEAT.md")

        # 10. Schedule first run (may already be set by fast-schedule above)
        if not state.pending_verification:
            self._schedule_next()

        # 11. Start scheduler
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="heartbeat-scheduler"
        )
        log.info(
            "Heartbeat extension started. interval=%ds, user=%s",
            self._interval,
            self._user_id,
        )

    async def stop(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        self.engine.services.pop("heartbeat", None)
        log.info("Heartbeat extension stopped.")

    async def health_check(self) -> dict:
        if self._store is None:
            return {"status": "error", "detail": "HeartbeatStore not initialized"}
        state = self._store.load_state()
        scheduler_alive = self._scheduler_task is not None and not self._scheduler_task.done()
        multiplier = self._get_backoff_multiplier()
        result = {
            "status": "ok"
            if scheduler_alive and state.enabled
            else "degraded"
            if not state.enabled
            else "error",
            "scheduler": "running" if scheduler_alive else "stopped",
            "enabled": state.enabled,
            "runs_today": state.runs_today,
            "consecutive_noop": state.consecutive_noop,
            "next_run": state.next_run,
            "interval": self._interval,
            "effective_interval": self._interval * multiplier,
            "backoff_multiplier": multiplier,
        }
        return result

    # -- bridge handler ------------------------------------------------------

    async def _handle_bridge_request(self, method: str, params: dict) -> dict | None:
        """Handle heartbeat bridge RPCs from MCP server processes."""
        handlers = {
            "heartbeat_trigger": self._handle_bridge_trigger,
            "heartbeat_dry_run": self._handle_bridge_dry_run,
            "heartbeat_set_verification": self._handle_bridge_set_verification,
        }
        handler = handlers.get(method)
        if handler is None:
            return None  # not ours
        try:
            return await handler(params)
        except Exception:
            log.exception("Bridge handler error for %s", method)
            return {"error": f"Internal error handling {method}"}

    async def _handle_bridge_trigger(self, params: dict) -> dict:
        """Handle heartbeat_trigger bridge RPC."""
        source = params.get("source", "session")
        event_type = params.get("event_type", "")
        urgency = params.get("urgency", "immediate")
        payload = params.get("payload")

        if not event_type:
            return {"error": "event_type is required"}

        self.trigger(source, event_type, urgency, payload)
        return {"ok": True, "urgency": urgency}

    async def _handle_bridge_dry_run(self, params: dict) -> dict:
        """Handle heartbeat_dry_run bridge RPC."""
        custom_instructions = params.get("instructions")
        return await self.dry_run_tier2(custom_instructions)

    async def _handle_bridge_set_verification(self, params: dict) -> dict:
        """Record or clear pending verification commit hash."""
        commit_hash = params.get("commit_hash")  # None to clear
        if commit_hash is not None and not re.match(r"^[0-9a-f]{7,40}$", commit_hash):
            return {"error": f"Invalid commit hash format: {commit_hash!r}"}
        self._store.update_state(pending_verification=commit_hash)
        # Defense-in-depth: clear restart marker when verification is cleared
        if commit_hash is None and self._restart_marker and self._restart_marker.exists():
            self._restart_marker.unlink(missing_ok=True)
        return {"ok": True, "pending_verification": commit_hash}

    # -- scheduler -----------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        try:
            while True:
                remaining = max(1, self._seconds_to_next_run())
                try:
                    trigger = await asyncio.wait_for(self._trigger_queue.get(), timeout=remaining)
                except TimeoutError:
                    trigger = None

                try:
                    if trigger:
                        await self._handle_trigger(trigger)
                    else:
                        await self._check_heartbeat()
                except Exception:
                    log.exception("Heartbeat scheduler error")
        except asyncio.CancelledError:
            return

    def _seconds_to_next_run(self) -> float:
        """Seconds until next scheduled timer run."""
        state = self._store.load_state()
        if not state.next_run:
            return float(self._interval)
        try:
            next_dt = datetime.fromisoformat(state.next_run)
            now = datetime.now(UTC)
            remaining = (next_dt - now).total_seconds()
            return max(1, remaining)
        except (ValueError, TypeError):
            return float(self._interval)

    # -- shared gate checks -------------------------------------------------

    def _check_daily_limit(self) -> tuple[bool, HeartbeatState]:
        """Reset daily counter if needed and check limit. Returns (allowed, state)."""
        state = self._store.load_state()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if state.runs_today_date != today:
            self._store.update_state(runs_today=0, runs_today_date=today)
            state.runs_today = 0
        if state.runs_today >= self._max_daily_runs:
            self._log_skipped("daily_limit", f"{state.runs_today}/{self._max_daily_runs}")
            return False, state
        return True, state

    # -- timer channel (Tier 0 + 1) ------------------------------------------

    async def _check_heartbeat(self) -> None:
        """Timer-channel entry point. Runs gate checks then Tier 2."""
        state = self._store.load_state()

        # Gate: enabled?
        if not state.enabled:
            self._schedule_next()
            return

        # Gate: daily reset + limit
        allowed, state = self._check_daily_limit()
        if not allowed:
            self._schedule_next()
            return

        # Gate: usage budget
        allowed, reason = await self._check_usage_budget(is_trigger=False)
        if not allowed:
            self._log_skipped("usage", reason)
            self._schedule_next()
            return

        # Gate: active hours
        if not self._in_active_hours():
            self._log_skipped("outside_active_hours")
            self._schedule_next()
            return

        # Gate: concurrent heartbeat
        if state.active_session_id:
            session = self.sm.sessions.get(state.active_session_id)
            if session and session.status == SessionStatus.BUSY:
                self._log_skipped("concurrent", f"session={state.active_session_id[:8]}")
                self._schedule_next()
                return
            # Stale — clear it
            self._store.update_state(active_session_id=None)

        # Tier 1: instructions non-empty?
        instructions = self._store.read_instructions()
        if not instructions or not instructions.strip():
            self._log_skipped("no_instructions")
            self._schedule_next()
            return

        # Collect pending normal events
        pending = self.drain_pending_events()

        # Tier 2: LLM decision
        await self._tier2_decision(instructions, pending_events=pending)
        self._schedule_next()

    # -- trigger channel -----------------------------------------------------

    async def _handle_trigger(self, trigger: TriggerEvent) -> None:
        """Trigger-channel entry point."""
        state = self._store.load_state()

        # Gate: enabled?
        if not state.enabled:
            return

        # Gate: daily reset + limit
        allowed, state = self._check_daily_limit()
        if not allowed:
            return

        # Gate: usage budget (immediate bypasses throttle but not pause)
        allowed, reason = await self._check_usage_budget(is_trigger=True)
        if not allowed:
            self._log_skipped("usage_trigger", reason)
            return

        # Gate: concurrent heartbeat
        if state.active_session_id:
            session = self.sm.sessions.get(state.active_session_id)
            if session and session.status == SessionStatus.BUSY:
                self._log_skipped("concurrent_trigger", f"session={state.active_session_id[:8]}")
                return
            self._store.update_state(active_session_id=None)

        instructions = self._store.read_instructions() or ""

        await self._tier2_decision(instructions, trigger=trigger)

    # -- Tier 2: LLM decision -----------------------------------------------

    def _build_tier2_prompt(
        self,
        instructions: str,
        pending_events: list[TriggerEvent] | None = None,
        trigger: TriggerEvent | None = None,
    ) -> str:
        """Build the Tier 2 prompt from instructions and context. No side effects."""
        context_parts = []

        # Memory summary
        memory_store = self.engine.services.get("memory")
        if memory_store and hasattr(memory_store, "read"):
            try:
                memory_content = memory_store.read("MEMORY.md")
                if memory_content:
                    lines = memory_content.splitlines()[:50]
                    context_parts.append("## Memory Summary\n" + "\n".join(lines))
            except Exception:
                pass

            # Self-improvement backlog
            try:
                backlog = memory_store.read("topics/backlog.md")
                if backlog:
                    context_parts.append("## Self-Improvement Backlog\n" + backlog)
            except Exception:
                pass

        # Recent events
        if self.engine.events:
            try:
                recent = self.engine.events.query(limit=10)
                if recent:
                    event_lines = []
                    for ev in recent:
                        event_lines.append(f"- [{ev.get('ts', '')}] {ev.get('type', '')}")
                    context_parts.append("## Recent Events\n" + "\n".join(event_lines))
            except Exception:
                pass

        # Accumulated normal events
        if pending_events:
            pe_lines = []
            for pe in pending_events:
                pe_lines.append(f"- [{pe.source}] {pe.event_type}: {pe.payload}")
            context_parts.append("## Pending Events\n" + "\n".join(pe_lines))

        # Trigger event
        if trigger:
            context_parts.append(
                f"## Trigger Event\n"
                f"Source: {trigger.source}\n"
                f"Type: {trigger.event_type}\n"
                f"Payload: {trigger.payload}"
            )

        # Pending verification (highest priority context)
        if self._store:
            pv_state = self._store.load_state()
            if pv_state.pending_verification:
                context_parts.insert(
                    0,
                    f"## PENDING VERIFICATION\n"
                    f"Commit {pv_state.pending_verification} was made by a previous "
                    f"heartbeat run and has not been verified yet. **Priority: verify "
                    f"this commit before picking new backlog items.**",
                )

        context_section = "\n\n".join(context_parts) if context_parts else ""

        return _TIER2_PROMPT_TEMPLATE.format(
            instructions=instructions,
            context_section=context_section,
        )

    async def dry_run_tier2(self, custom_instructions: str | None = None) -> dict:
        """Simulate a Tier 2 decision without side effects.

        Builds the full Tier 2 prompt, calls engine.ask(), and returns the
        decision without modifying state or executing Tier 3.

        Returns dict with: decision, would_execute, noop, prompt.
        """
        if self._store is None:
            return {"error": "HeartbeatStore not initialized"}

        instructions = custom_instructions or self._store.read_instructions() or ""
        if not instructions.strip():
            return {"error": "No instructions available (HEARTBEAT.md empty or missing)"}

        prompt = self._build_tier2_prompt(instructions)

        try:
            response = await self.engine.ask(
                prompt=prompt,
                cwd=self._working_dir,
                timeout=120,
            )
        except Exception as e:
            return {"error": f"engine.ask failed: {e}", "prompt": prompt}

        if response.startswith("[Error]"):
            return {"error": response, "prompt": prompt}

        decision = response.strip()
        noop = "NOTHING" in decision.upper() and len(decision) <= _NOOP_MAX_LEN

        return {
            "decision": decision,
            "would_execute": not noop,
            "noop": noop,
            "prompt": prompt,
        }

    async def _tier2_decision(
        self,
        instructions: str,
        pending_events: list[TriggerEvent] | None = None,
        trigger: TriggerEvent | None = None,
    ) -> None:
        """Ask Claude whether action is needed. Low-cost engine.ask() call."""
        prompt = self._build_tier2_prompt(instructions, pending_events, trigger)

        # Update counters
        now_iso = datetime.now(UTC).isoformat()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        state = self._store.load_state()

        # Daily reset check
        if state.runs_today_date != today:
            runs_today = 1
        else:
            runs_today = state.runs_today + 1

        self._store.update_state(
            last_run=now_iso,
            run_count=state.run_count + 1,
            runs_today=runs_today,
            runs_today_date=today,
        )

        # Call LLM
        try:
            response = await self.engine.ask(
                prompt=prompt,
                cwd=self._working_dir,
                timeout=120,
            )
        except Exception as e:
            log.error("Tier 2 decision error: %s", e)
            response = f"[Error] {e}"

        # Check for error
        if response.startswith("[Error]"):
            log.warning("Tier 2 failed: %s", response[:200])
            if self.engine.events:
                self.engine.events.log(
                    "heartbeat.skipped",
                    detail={
                        "reason": "tier2_error",
                        "error": response[:200],
                    },
                )
            return

        decision = response.strip()

        if "NOTHING" in decision.upper() and len(decision) <= _NOOP_MAX_LEN:
            state = self._store.load_state()
            self._store.update_state(consecutive_noop=state.consecutive_noop + 1)
            if self.engine.events:
                self.engine.events.log(
                    "heartbeat.noop",
                    detail={
                        "consecutive": state.consecutive_noop + 1,
                    },
                )
            return

        # Action needed — reset noop counter and execute
        self._store.update_state(consecutive_noop=0)
        if self.engine.events:
            self.engine.events.log(
                "heartbeat.decided",
                detail={
                    "decision": decision[:200],
                    "trigger_source": trigger.source if trigger else None,
                },
            )
        await self._tier3_execute(decision, instructions, trigger)

    # -- Tier 3: full session execution --------------------------------------

    async def _tier3_execute(
        self,
        decision: str,
        instructions: str,
        trigger: TriggerEvent | None = None,
    ) -> None:
        """Create a full session to execute the decided task."""
        user_id = self._user_id

        # Check slot availability; try to reclaim finished heartbeat/cron sessions
        user_sessions = self.sm.get_sessions_for_user(user_id)
        if len(user_sessions) >= self.sm.max_sessions_per_user:
            reclaimed = await self._reclaim_session(user_id)
            if not reclaimed:
                log.warning("Heartbeat Tier 3 deferred: no slots for user %s", user_id)
                self._log_skipped("no_slots")
                return

        # Build context
        context = dict(self._notify_context)
        context["heartbeat_auto_cleanup"] = True
        context["heartbeat_run"] = True

        state = self._store.load_state()

        try:
            session = await self.sm.create_session(
                name=f"heartbeat-{state.run_count}",
                user_id=user_id,
                working_dir=self._working_dir,
                context=context,
            )
        except RuntimeError as e:
            log.error("Cannot create heartbeat session: %s", e)
            return

        # Record active session
        self._store.update_state(active_session_id=session.id)

        if self.engine.events:
            self.engine.events.log(
                "heartbeat.started",
                session_id=session.id,
                detail={
                    "decision": decision[:200],
                },
            )

        # Build Tier 3 prompt
        trigger_context = ""
        if trigger:
            trigger_context = (
                f"## Trigger\n"
                f"Source: {trigger.source}\n"
                f"Type: {trigger.event_type}\n"
                f"Payload: {trigger.payload}"
            )

        prompt = _TIER3_PROMPT_TEMPLATE.format(
            run_count=state.run_count,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            decision=decision,
            instructions=instructions,
            trigger_context=trigger_context,
        )

        await self.sm.send_prompt(session.id, prompt)
        log.info("Heartbeat Tier 3 dispatched to session %s: %s", session.id[:8], decision[:80])

    # -- delivery callback ---------------------------------------------------

    async def _on_delivery(self, session_id: str, result_text: str, metadata: dict) -> None:
        """Track heartbeat session completion and auto-cleanup."""
        session = self.sm.sessions.get(session_id)
        if not session:
            return
        if not session.context.get("heartbeat_run"):
            return

        # Only care about final results
        if not metadata.get("is_final"):
            return

        # Clear active session
        self._store.update_state(active_session_id=None)

        cost = metadata.get("total_cost_usd")
        if self.engine.events:
            self.engine.events.log(
                "heartbeat.completed",
                session_id=session_id,
                detail={
                    "cost_usd": cost,
                },
            )

        # Auto-cleanup after delay
        if session.context.get("heartbeat_auto_cleanup"):
            asyncio.create_task(self._delayed_cleanup(session_id))

    async def _delayed_cleanup(self, session_id: str) -> None:
        """Destroy a heartbeat session after a brief delay."""
        await asyncio.sleep(_CLEANUP_DELAY)
        session = self.sm.sessions.get(session_id)
        if session and session.status in (SessionStatus.IDLE, SessionStatus.STOPPED):
            await self.sm.destroy_session(session_id)
            log.info("Auto-cleaned heartbeat session %s", session_id[:8])

    # -- usage budget --------------------------------------------------------

    async def _check_usage_budget(self, is_trigger: bool = False) -> tuple[bool, str]:
        """Check if usage windows allow a heartbeat run."""
        now = time.monotonic()
        if self._usage_cache and (now - self._usage_cache_ts) < _USAGE_CACHE_TTL:
            usage = self._usage_cache
        else:
            usage = await get_usage()
            self._usage_cache = usage
            self._usage_cache_ts = now

        if "error" in usage:
            return True, ""  # Fail open

        util_5h = (usage.get("five_hour") or {}).get("utilization", 0)
        util_7d = (usage.get("seven_day") or {}).get("utilization", 0)
        max_util = max(util_5h, util_7d)

        # Hard stop: >= pause_threshold
        if max_util >= self._usage_pause:
            return False, f"usage_critical ({util_5h:.0f}%/5h, {util_7d:.0f}%/7d)"

        # Throttle: >= throttle_threshold, only allow immediate triggers
        if max_util >= self._usage_throttle and not is_trigger:
            return False, f"usage_high ({util_5h:.0f}%/5h, {util_7d:.0f}%/7d)"

        return True, ""

    # -- adaptive backoff ----------------------------------------------------

    def _get_backoff_multiplier(self) -> int:
        """Get interval multiplier based on consecutive noop count."""
        state = self._store.load_state()
        noop = state.consecutive_noop
        for threshold, multiplier in _BACKOFF_TABLE:
            if noop <= threshold:
                return multiplier
        return _BACKOFF_MAX_MULTIPLIER

    def _schedule_next(self) -> None:
        """Compute and persist the next timer run."""
        multiplier = self._get_backoff_multiplier()
        interval = self._interval * multiplier
        next_run = datetime.now(UTC).timestamp() + interval
        next_iso = datetime.fromtimestamp(next_run, tz=UTC).isoformat()
        self._store.update_state(next_run=next_iso)

    # -- active hours --------------------------------------------------------

    def _in_active_hours(self) -> bool:
        """Check if current UTC time is within configured active hours."""
        if not self._active_hours:
            return True
        try:
            parts = self._active_hours.split("-")
            if len(parts) != 2:
                return True
            start_h, start_m = map(int, parts[0].split(":"))
            end_h, end_m = map(int, parts[1].split(":"))
        except (ValueError, IndexError):
            log.warning("Invalid active_hours format: %s", self._active_hours)
            return True

        now = datetime.now(UTC)
        now_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes <= end_minutes:
            # Same-day window: e.g. 08:00-22:00
            return start_minutes <= now_minutes < end_minutes
        else:
            # Cross-midnight: e.g. 22:00-06:00
            return now_minutes >= start_minutes or now_minutes < end_minutes

    # -- slot reclamation ----------------------------------------------------

    async def _reclaim_session(self, user_id: str) -> bool:
        """Try to destroy an idle heartbeat/cron session to free a slot."""
        for s in self.sm.get_sessions_for_user(user_id):
            if (
                s.context.get("heartbeat_auto_cleanup") or s.context.get("cron_auto_cleanup")
            ) and s.status in (SessionStatus.IDLE, SessionStatus.STOPPED):
                await self.sm.destroy_session(s.id)
                log.info("Reclaimed session %s to free slot for heartbeat", s.id[:8])
                return True
        return False

    # -- revert failure handling ---------------------------------------------

    def _handle_revert_failure(self, commit: str, stderr_text: str) -> None:
        """Handle a failed git revert: update backlog and schedule telegram notification."""
        # Update backlog
        memory = self.engine.services.get("memory")
        if memory and hasattr(memory, "read") and hasattr(memory, "write"):
            try:
                err_short = stderr_text.strip()[:200]
                new_item = (
                    f"- [ ] [L3] [REVERT-FAILED] commit {commit[:12]} could not be "
                    f"auto-reverted ({err_short}). Manually resolve conflict and re-do the fix.\n"
                )
                content = memory.read("topics/backlog.md") or ""
                if "## Pending" in content:
                    content = content.replace("## Pending\n", f"## Pending\n{new_item}", 1)
                else:
                    content += f"\n## Pending\n{new_item}"
                memory.write("topics/backlog.md", content)
                log.info("Added REVERT-FAILED item to backlog for commit %s", commit[:12])
            except Exception:
                log.exception("Failed to update backlog after revert failure")

        # Schedule deferred telegram notification (after startup completes)
        asyncio.create_task(self._notify_revert_failure(commit, stderr_text))

    async def _notify_revert_failure(self, commit: str, stderr_text: str) -> None:
        """Send a telegram notification about a failed auto-revert."""
        await asyncio.sleep(15)  # Allow startup to complete and telegram to register
        chat_id = self._notify_context.get("chat_id")
        if not chat_id:
            return
        telegram = self.engine.services.get("telegram")
        if not telegram:
            log.warning("Cannot notify revert failure: telegram not in services")
            return
        msg = (
            f"WARNING: REVERT FAILED\n"
            f"Commit {commit[:12]} could not be auto-reverted on double-restart.\n"
            f"Error: {stderr_text.strip()[:300]}\n"
            f"A recovery item has been added to the backlog."
        )
        try:
            await telegram.notify(chat_id, msg)
        except Exception:
            log.exception("Failed to send revert-failure telegram notification")

    # -- logging helpers -----------------------------------------------------

    def _log_skipped(self, reason: str, detail: str = "") -> None:
        log.debug("Heartbeat skipped: %s %s", reason, detail)
        if self.engine.events:
            self.engine.events.log(
                "heartbeat.skipped",
                detail={
                    "reason": reason,
                    "detail": detail,
                },
            )
