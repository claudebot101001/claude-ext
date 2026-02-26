"""Cron job storage with file-based persistence and locking."""

import fcntl
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class CronJob:
    id: str
    name: str
    prompt: str
    working_dir: str
    user_id: str

    # Trigger (exactly one should be set)
    cron_expr: str | None = None     # "0 8 * * *" — recurring
    run_at: str | None = None        # ISO timestamp — one-time

    # Session strategy
    session_strategy: str = "new"    # "new" | "reuse"
    session_id: str | None = None    # our session ID (reuse target)
    claude_session_id: str | None = None  # Claude CLI session UUID (for --resume fallback)

    # Notification routing (merged into new session's context)
    notify_context: dict = field(default_factory=dict)

    # State
    enabled: bool = True
    created_by: str = ""             # source session_id or "config"
    last_run: str | None = None      # ISO timestamp
    next_run: str | None = None      # ISO timestamp
    created_at: str = ""


def parse_relative_time(expr: str) -> datetime | None:
    """Parse relative time expressions like '+20m', '+1h', '+1h30m', '+2d'."""
    expr = expr.strip()
    if not expr.startswith("+"):
        return None

    body = expr[1:]
    total = timedelta()
    num_buf = ""
    for ch in body:
        if ch.isdigit():
            num_buf += ch
        elif ch in ("d", "h", "m", "s") and num_buf:
            n = int(num_buf)
            if ch == "d":
                total += timedelta(days=n)
            elif ch == "h":
                total += timedelta(hours=n)
            elif ch == "m":
                total += timedelta(minutes=n)
            elif ch == "s":
                total += timedelta(seconds=n)
            num_buf = ""
        else:
            return None

    if num_buf:
        # Bare number without unit — treat as minutes
        total += timedelta(minutes=int(num_buf))

    if total.total_seconds() <= 0:
        return None

    return datetime.now(timezone.utc) + total


def compute_next_run(cron_expr: str, after: datetime | None = None) -> str | None:
    """Compute next run time from a cron expression. Returns ISO string."""
    try:
        from croniter import croniter
    except ImportError:
        log.error("croniter not installed — cannot evaluate cron expressions")
        return None

    if after is None:
        after = datetime.now(timezone.utc)
    cron = croniter(cron_expr, after)
    nxt = cron.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return nxt.isoformat()


class JobStore:
    """Thread/process-safe job store backed by a JSON file with flock."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- low-level I/O with locking -----------------------------------------

    def _read_locked(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _write_locked(self, jobs: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(jobs, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        tmp.rename(self.path)

    # -- CRUD ---------------------------------------------------------------

    def list_jobs(self, user_id: str | None = None) -> list[CronJob]:
        raw = self._read_locked()
        jobs = [self._from_dict(d) for d in raw]
        if user_id:
            jobs = [j for j in jobs if j.user_id == user_id]
        return jobs

    def get_job(self, job_id: str) -> CronJob | None:
        for d in self._read_locked():
            if d.get("id") == job_id:
                return self._from_dict(d)
        return None

    def add_job(self, job: CronJob) -> None:
        jobs = self._read_locked()
        jobs.append(asdict(job))
        self._write_locked(jobs)
        log.info("Stored cron job: %s (%s)", job.name, job.id[:8])

    def update_job(self, job_id: str, **fields) -> bool:
        jobs = self._read_locked()
        for d in jobs:
            if d.get("id") == job_id:
                d.update(fields)
                self._write_locked(jobs)
                return True
        return False

    def delete_job(self, job_id: str) -> bool:
        jobs = self._read_locked()
        before = len(jobs)
        jobs = [d for d in jobs if d.get("id") != job_id]
        if len(jobs) < before:
            self._write_locked(jobs)
            return True
        return False

    def get_due_jobs(self, now: datetime | None = None) -> list[CronJob]:
        """Return enabled jobs whose next_run is <= now."""
        if now is None:
            now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        result = []
        for d in self._read_locked():
            if not d.get("enabled", True):
                continue
            nr = d.get("next_run")
            if nr and nr <= now_iso:
                result.append(self._from_dict(d))
        return result

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def create_job(
        name: str,
        prompt: str,
        working_dir: str,
        user_id: str,
        cron_expr: str | None = None,
        run_at: str | None = None,
        session_strategy: str = "new",
        session_id: str | None = None,
        claude_session_id: str | None = None,
        notify_context: dict | None = None,
        created_by: str = "",
    ) -> CronJob:
        now = datetime.now(timezone.utc)

        # Compute next_run
        if run_at:
            next_run = run_at
        elif cron_expr:
            next_run = compute_next_run(cron_expr, now)
        else:
            next_run = None

        return CronJob(
            id=str(uuid.uuid4()),
            name=name,
            prompt=prompt,
            working_dir=working_dir,
            user_id=user_id,
            cron_expr=cron_expr,
            run_at=run_at,
            session_strategy=session_strategy,
            session_id=session_id,
            claude_session_id=claude_session_id,
            notify_context=notify_context or {},
            created_by=created_by,
            next_run=next_run,
            created_at=now.isoformat(),
        )

    @staticmethod
    def _from_dict(d: dict) -> CronJob:
        return CronJob(
            id=d["id"],
            name=d.get("name", ""),
            prompt=d.get("prompt", ""),
            working_dir=d.get("working_dir", ""),
            user_id=d.get("user_id", ""),
            cron_expr=d.get("cron_expr"),
            run_at=d.get("run_at"),
            session_strategy=d.get("session_strategy", "new"),
            session_id=d.get("session_id"),
            claude_session_id=d.get("claude_session_id"),
            notify_context=d.get("notify_context", {}),
            enabled=d.get("enabled", True),
            created_by=d.get("created_by", ""),
            last_run=d.get("last_run"),
            next_run=d.get("next_run"),
            created_at=d.get("created_at", ""),
        )
