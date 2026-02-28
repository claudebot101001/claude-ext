"""Cron extension — scheduled task execution for Claude Code sessions.

Two roles:
1. Scheduler: triggers jobs at configured times, creates/reuses sessions.
2. MCP provider: registers an MCP server so Claude can create jobs dynamically.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.extension import Extension
from core.session import SessionStatus
from extensions.cron.store import CronJob, JobStore, compute_next_run

log = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 15  # seconds between due-job checks


class ExtensionImpl(Extension):
    name = "cron"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._scheduler_task: asyncio.Task | None = None
        # Store lives alongside session state
        store_path = Path(self.engine.session_manager.base_dir) / "cron_jobs.json"
        self.store = JobStore(store_path)
        self._static_jobs = config.get("jobs", [])

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # Register MCP server so Claude sessions can call cron_create etc.
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        self.sm.register_mcp_server("cron", {
            "command": sys.executable,
            "args": [mcp_script],
            "env": {
                "CRON_STORE_PATH": str(self.store.path),
            },
        }, tools=[
            {"name": "cron_create", "description": "Create a scheduled task (cron or one-time)"},
            {"name": "cron_list", "description": "List all cron jobs for current user"},
            {"name": "cron_delete", "description": "Delete a cron job by ID"},
            {"name": "cron_status", "description": "Get detailed status of a cron job"},
        ])

        # Register delivery callback to track job completion
        self.sm.add_delivery_callback(self._on_delivery)

        # Load static jobs from config (idempotent — skips if already exists)
        self._load_static_jobs()

        # Start scheduler loop
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="cron-scheduler"
        )
        log.info("Cron extension started. %d job(s) in store.", len(self.store.list_jobs()))

    async def stop(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        log.info("Cron extension stopped.")

    async def health_check(self) -> dict:
        scheduler_alive = (
            self._scheduler_task is not None
            and not self._scheduler_task.done()
        )
        jobs = self.store.list_jobs()
        return {
            "status": "ok" if scheduler_alive else "error",
            "scheduler": "running" if scheduler_alive else "stopped",
            "jobs": len(jobs),
        }

    # -- static job loading -------------------------------------------------

    def _load_static_jobs(self) -> None:
        """Load jobs defined in config.yaml. Uses name as idempotency key."""
        existing_names = {j.name for j in self.store.list_jobs() if j.created_by == "config"}
        for jdef in self._static_jobs:
            name = jdef.get("name", "")
            if not name or name in existing_names:
                continue
            job = self.store.create_job(
                name=name,
                prompt=jdef["prompt"],
                working_dir=jdef.get("working_dir", os.getcwd()),
                user_id=jdef.get("user_id", "system"),
                cron_expr=jdef.get("cron_expr"),
                run_at=jdef.get("run_at"),
                session_strategy=jdef.get("session_strategy", "new"),
                notify_context=jdef.get("notify_context", {}),
                created_by="config",
            )
            self.store.add_job(job)
            log.info("Loaded static cron job: %s", name)

    # -- scheduler loop -----------------------------------------------------

    async def _scheduler_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(SCHEDULER_INTERVAL)
                try:
                    await self._check_due_jobs()
                except Exception:
                    log.exception("Error in cron scheduler check")
        except asyncio.CancelledError:
            return

    async def _check_due_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        due = self.store.get_due_jobs(now)

        for job in due:
            log.info("Cron job due: %s (%s)", job.name, job.id[:8])
            try:
                await self._execute_job(job)
            except Exception:
                log.exception("Failed to execute cron job %s", job.name)

            # Update last_run and compute next_run
            updates = {"last_run": now.isoformat()}
            if job.cron_expr:
                updates["next_run"] = compute_next_run(job.cron_expr, now)
            else:
                # One-time job: disable after execution
                updates["next_run"] = None
                updates["enabled"] = False
            self.store.update_job(job.id, **updates)

    # -- job execution ------------------------------------------------------

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a cron job by creating/reusing a session and sending the prompt."""
        if self.engine.events:
            self.engine.events.log("cron.triggered", detail={
                "job_id": job.id, "name": job.name,
                "strategy": job.session_strategy,
            })

        if job.session_strategy == "reuse":
            await self._execute_reuse(job)
        else:
            await self._execute_new(job)

    async def _execute_new(self, job: CronJob) -> None:
        """Create a fresh session for this job."""
        user_id = job.user_id

        # Build context: merge notification routing
        context = dict(job.notify_context)
        context["cron_job_id"] = job.id

        # Check slot availability; try to reclaim finished cron sessions first
        user_sessions = self.sm.get_sessions_for_user(user_id)
        if len(user_sessions) >= self.sm.max_sessions_per_user:
            reclaimed = await self._reclaim_cron_session(user_id)
            if not reclaimed:
                log.warning(
                    "Cron job '%s' deferred: no slots available for user %s",
                    job.name, user_id,
                )
                # Nudge next_run forward by one interval to retry
                retry = datetime.now(timezone.utc)
                self.store.update_job(job.id, next_run=retry.isoformat())
                return

        try:
            session = await self.sm.create_session(
                name=f"cron-{job.name[:20]}",
                user_id=user_id,
                working_dir=job.working_dir,
                context=context,
            )
        except RuntimeError as e:
            log.error("Cannot create session for cron job '%s': %s", job.name, e)
            return

        # Tag session for auto-cleanup
        session.context["cron_auto_cleanup"] = True

        await self.sm.send_prompt(session.id, job.prompt)
        log.info("Cron job '%s' dispatched to new session %s", job.name, session.id[:8])

    async def _execute_reuse(self, job: CronJob) -> None:
        """Reuse an existing session, with fallback if deleted."""
        session = self.sm.sessions.get(job.session_id) if job.session_id else None

        if session:
            # Session exists — send prompt directly
            if session.status == SessionStatus.DEAD:
                log.warning("Cron job '%s': target session is dead, falling back to resume", job.name)
                await self._fallback_resume(job)
                return

            # Ensure cron_job_id is in context for delivery tracking
            session.context["cron_job_id"] = job.id

            await self.sm.send_prompt(session.id, job.prompt)
            log.info(
                "Cron job '%s' dispatched to existing session %s",
                job.name, session.id[:8],
            )
        else:
            # Session deleted — fallback: create new session + --resume
            log.warning(
                "Cron job '%s': session %s not found, attempting resume fallback",
                job.name, (job.session_id or "")[:8],
            )
            await self._fallback_resume(job)

    async def _fallback_resume(self, job: CronJob) -> None:
        """Create a new session that resumes the old Claude CLI session."""
        context = dict(job.notify_context)
        context["cron_job_id"] = job.id

        try:
            session = await self.sm.create_session(
                name=f"cron-{job.name[:20]}",
                user_id=job.user_id,
                working_dir=job.working_dir,
                context=context,
            )
        except RuntimeError as e:
            log.error("Fallback session creation failed for '%s': %s", job.name, e)
            return

        # Transplant the old Claude session ID so --resume picks up context.
        # Set prompt_count > 0 so _generate_run_script uses --resume instead of --session-id.
        if job.claude_session_id:
            session.claude_session_id = job.claude_session_id
            session.prompt_count = 1  # force --resume on next execution

        session.context["cron_auto_cleanup"] = True

        # Prepend fallback note so the user sees it in the result
        prompt = (
            "[System note: The original session for this scheduled task was deleted. "
            "Claude context has been resumed in a new session.]\n\n"
            + job.prompt
        )
        await self.sm.send_prompt(session.id, prompt)
        log.info(
            "Cron job '%s' dispatched via resume fallback (claude session %s)",
            job.name, (job.claude_session_id or "")[:8],
        )

    async def _reclaim_cron_session(self, user_id: str) -> bool:
        """Try to destroy an idle cron-created session to free a slot."""
        for s in self.sm.get_sessions_for_user(user_id):
            if (
                s.context.get("cron_auto_cleanup")
                and s.status in (SessionStatus.IDLE, SessionStatus.STOPPED)
            ):
                await self.sm.destroy_session(s.id)
                log.info("Reclaimed cron session %s to free slot", s.id[:8])
                return True
        return False

    # -- delivery callback --------------------------------------------------

    async def _on_delivery(self, session_id: str, result_text: str, metadata: dict) -> None:
        """Track cron job completion. Auto-cleanup new-strategy sessions."""
        session = self.sm.sessions.get(session_id)
        if not session:
            return
        job_id = session.context.get("cron_job_id")
        if not job_id:
            return  # Not a cron-triggered session

        # Skip heartbeats and streaming events — cron only cares about final results
        if metadata.get("is_heartbeat") or metadata.get("is_stream"):
            return
        if not metadata.get("is_final"):
            return

        # Update job's last_run in store
        now = datetime.now(timezone.utc).isoformat()
        self.store.update_job(job_id, last_run=now)

        # Auto-cleanup: destroy sessions created with "new" strategy after delivery
        if session.context.get("cron_auto_cleanup"):
            # Give a short delay for delivery to complete
            asyncio.create_task(self._delayed_cleanup(session_id))

    async def _delayed_cleanup(self, session_id: str) -> None:
        """Destroy a cron-created session after a brief delay."""
        await asyncio.sleep(5)
        session = self.sm.sessions.get(session_id)
        if session and session.status in (SessionStatus.IDLE, SessionStatus.STOPPED):
            await self.sm.destroy_session(session_id)
            log.info("Auto-cleaned cron session %s", session_id[:8])

