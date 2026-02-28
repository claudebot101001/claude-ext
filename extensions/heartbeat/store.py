"""Heartbeat state and instruction file storage.

Pure file I/O layer with flock-based concurrency control.  No asyncio objects
— the MCP server process also instantiates HeartbeatStore (sync stdin/stdout
JSON-RPC, no event loop), consistent with MemoryStore / VaultStore / JobStore.

Storage layout::

    {state_dir}/heartbeat/
    ├── state.json         # Scheduler state (JSON + flock)
    ├── HEARTBEAT.md       # Standing instructions (Markdown)
    └── heartbeat.lock     # Unified lockfile (LOCK_SH read / LOCK_EX write)

Thread/process safety: unified lockfile.  Read-only ops take LOCK_SH;
mutations hold LOCK_EX across the full read-modify-write cycle.
Writes use atomic temp+rename.
"""

import contextlib
import fcntl
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK_FILE = "heartbeat.lock"
_STATE_FILE = "state.json"
_INSTRUCTIONS_FILE = "HEARTBEAT.md"


@dataclass
class HeartbeatState:
    enabled: bool = True
    last_run: str | None = None          # ISO (most recent Tier 2 call)
    next_run: str | None = None          # ISO (next timer expiry)
    run_count: int = 0                   # Total Tier 2 call count
    runs_today: int = 0                  # Tier 2 calls today
    runs_today_date: str | None = None   # YYYY-MM-DD
    consecutive_noop: int = 0            # Consecutive NOTHING decisions
    active_session_id: str | None = None # Current Tier 3 session


class HeartbeatStore:
    """File-backed state and instruction storage for the heartbeat scheduler.

    Usage::

        store = HeartbeatStore(Path("~/.claude-ext/heartbeat"))
        state = store.load_state()
        store.update_state(enabled=False)
        instructions = store.read_instructions()
        store.write_instructions("# Check deployments daily")
    """

    def __init__(self, heartbeat_dir: Path):
        self.heartbeat_dir = heartbeat_dir
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.heartbeat_dir / _LOCK_FILE
        self._state_path = self.heartbeat_dir / _STATE_FILE
        self._instructions_path = self.heartbeat_dir / _INSTRUCTIONS_FILE
        log.info("HeartbeatStore initialized at %s", self.heartbeat_dir)

    # -- state CRUD ----------------------------------------------------------

    def load_state(self) -> HeartbeatState:
        """Load scheduler state from disk. Returns defaults on missing/corrupt."""
        with self._shared_lock():
            return self._read_state_unlocked()

    def save_state(self, state: HeartbeatState) -> None:
        """Atomically write full state to disk."""
        with self._exclusive_lock():
            self._write_state_unlocked(state)

    def update_state(self, **fields) -> HeartbeatState:
        """Read-modify-write: update only the specified fields.

        Returns the updated state.  Holds LOCK_EX across the full cycle.
        """
        with self._exclusive_lock():
            state = self._read_state_unlocked()
            for key, value in fields.items():
                if hasattr(state, key):
                    setattr(state, key, value)
                else:
                    log.warning("HeartbeatState has no field '%s', ignoring", key)
            self._write_state_unlocked(state)
            return state

    # -- instructions I/O ----------------------------------------------------

    def read_instructions(self) -> str | None:
        """Read HEARTBEAT.md. Returns None if file doesn't exist."""
        with self._shared_lock():
            if not self._instructions_path.exists():
                return None
            return self._instructions_path.read_text(encoding="utf-8")

    def write_instructions(self, content: str) -> int:
        """Atomically overwrite HEARTBEAT.md. Returns bytes written."""
        with self._exclusive_lock():
            data = content.encode("utf-8")
            tmp = self._instructions_path.with_suffix(".md.tmp")
            tmp.write_bytes(data)
            tmp.rename(self._instructions_path)
        log.info("HeartbeatStore: wrote %d bytes to HEARTBEAT.md", len(data))
        return len(data)

    # -- internal I/O (caller must hold appropriate lock) --------------------

    def _read_state_unlocked(self) -> HeartbeatState:
        if not self._state_path.exists():
            return HeartbeatState()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            # Only accept known fields
            known = {f.name for f in HeartbeatState.__dataclass_fields__.values()}
            filtered = {k: v for k, v in raw.items() if k in known}
            return HeartbeatState(**filtered)
        except (json.JSONDecodeError, TypeError, OSError) as e:
            log.warning("Corrupt heartbeat state, resetting: %s", e)
            return HeartbeatState()

    def _write_state_unlocked(self, state: HeartbeatState) -> None:
        data = json.dumps(asdict(state), indent=2).encode("utf-8")
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.rename(self._state_path)

    # -- locking -------------------------------------------------------------

    @contextlib.contextmanager
    def _shared_lock(self):
        """LOCK_SH on the unified lockfile."""
        f = open(self._lock_path, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    @contextlib.contextmanager
    def _exclusive_lock(self):
        """LOCK_EX on the unified lockfile."""
        f = open(self._lock_path, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()
