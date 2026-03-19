"""Structured event log — append-only JSONL with query and rotation.

Best-effort: ``log()`` never raises; failures are warned but swallowed.
"""

import fcntl
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB rotation threshold


class EventLog:
    """Append-only structured event log backed by a JSONL file.

    Each line: ``{"ts": "ISO8601", "type": "dotted.name", "session_id": "...", "detail": {...}}``

    Concurrency: ``events.lock`` with LOCK_SH (query) / LOCK_EX (log).
    Rotation: single-generation rename to ``.1`` when file exceeds 10 MB.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.parent / "events.lock"

    # -- public API ---------------------------------------------------------

    def log(
        self, event_type: str, session_id: str | None = None, detail: dict | None = None
    ) -> None:
        """Append an event.  Best-effort — swallows OSError."""
        try:
            entry = {
                "ts": datetime.now(UTC).isoformat(),
                "type": event_type,
                "session_id": session_id,
                "detail": detail or {},
            }
            line = json.dumps(entry, separators=(",", ":")) + "\n"

            with open(self._lock_path, "a") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    self._maybe_rotate()
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(line)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except OSError:
            log.warning("EventLog.log failed", exc_info=True)

    def query(
        self,
        event_type: str | None = None,
        session_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Read matching events from the current log file.

        Filters are ANDed.  *since* is an ISO 8601 timestamp string.
        Returns newest-first (reversed), capped at *limit*.
        """
        if not self.path.exists():
            return []

        results: list[dict] = []
        with open(self._lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            try:
                lines = self._read_lines()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if event_type and entry.get("type") != event_type:
                continue
            if session_id and entry.get("session_id") != session_id:
                continue
            if since and entry.get("ts", "") < since:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    # -- internals ----------------------------------------------------------

    def _read_lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    def _maybe_rotate(self) -> None:
        """Rotate to .1 if file exceeds threshold.  Caller holds LOCK_EX."""
        try:
            if self.path.exists() and self.path.stat().st_size >= _MAX_FILE_SIZE:
                rotated = self.path.with_suffix(self.path.suffix + ".1")
                os.replace(self.path, rotated)
                log.info("Rotated event log to %s", rotated)
        except OSError:
            pass
