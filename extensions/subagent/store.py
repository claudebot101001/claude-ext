"""Sub-agent state storage with file-based persistence and locking.

Storage layout::

    {state_dir}/subagent/
    ├── agents.json         # Sub-agent records (JSON array + flock)
    └── subagent.lock       # Unified lockfile (LOCK_SH read / LOCK_EX write)

Thread/process safety: unified lockfile.  Read-only ops take LOCK_SH;
mutations hold LOCK_EX across the full read-modify-write cycle.
Writes use atomic temp+rename.
"""

import contextlib
import fcntl
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK_FILE = "subagent.lock"
_AGENTS_FILE = "agents.json"

# Truncation limit for result summaries stored in the JSON file.
MAX_RESULT_LENGTH = 2000


@dataclass
class SubAgent:
    """Record of a spawned sub-agent worker session."""

    id: str  # = session_id from SessionManager
    parent_session_id: str  # creator session
    name: str  # human-readable label
    task: str  # original task description
    paradigm: str  # "coder" | "reviewer" | "researcher" | custom
    user_id: str  # inherited from parent
    working_dir: str  # worktree path or parent's working_dir

    # Worktree
    worktree_enabled: bool = False
    worktree_path: str | None = None
    worktree_branch: str | None = None
    parent_branch: str | None = None  # branch worktree was forked from

    # Lifecycle
    status: str = "pending"  # pending → running → completed/failed/stopped → merged
    created_at: str = ""
    completed_at: str | None = None

    # Results
    result_summary: str | None = None  # final delivery text (truncated)
    cost_usd: float | None = None
    error: str | None = None


class SubAgentStore:
    """File-backed sub-agent record storage with flock concurrency control.

    Usage::

        store = SubAgentStore(Path("~/.claude-ext/subagent"))
        store.add_agent(SubAgent(id="abc", ...))
        agents = store.list_agents(parent_session_id="xyz")
        store.update_agent("abc", status="completed")
    """

    def __init__(self, subagent_dir: Path):
        self.subagent_dir = subagent_dir
        self.subagent_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.subagent_dir / _LOCK_FILE
        self._agents_path = self.subagent_dir / _AGENTS_FILE
        log.info("SubAgentStore initialized at %s", self.subagent_dir)

    # -- CRUD ---------------------------------------------------------------

    def list_agents(self, parent_session_id: str | None = None) -> list[SubAgent]:
        """List all agents, optionally filtered by parent session."""
        raw = self._read_locked()
        agents = [self._from_dict(d) for d in raw]
        if parent_session_id:
            agents = [a for a in agents if a.parent_session_id == parent_session_id]
        return agents

    def get_agent(self, agent_id: str) -> SubAgent | None:
        """Get a single agent by ID (exact match or unique prefix)."""
        agents = self._read_locked()
        # Exact match first
        for d in agents:
            if d.get("id") == agent_id:
                return self._from_dict(d)
        # Prefix match (for truncated IDs from MCP display)
        if len(agent_id) >= 6:
            matches = [d for d in agents if d.get("id", "").startswith(agent_id)]
            if len(matches) == 1:
                return self._from_dict(matches[0])
        return None

    def resolve_agent_id(self, partial_id: str) -> str | None:
        """Resolve a partial/truncated agent ID to the full ID.

        Returns full ID if exactly one agent matches the prefix, else None.
        """
        agents = self._read_locked()
        for d in agents:
            if d.get("id") == partial_id:
                return partial_id  # exact
        if len(partial_id) >= 6:
            matches = [d.get("id") for d in agents if d.get("id", "").startswith(partial_id)]
            if len(matches) == 1:
                return matches[0]
        return None

    def add_agent(self, agent: SubAgent) -> None:
        """Add a new agent record."""
        with self._exclusive_lock():
            agents = self._read_unlocked()
            agents.append(asdict(agent))
            self._write_unlocked(agents)
        log.info("Stored sub-agent: %s (%s)", agent.name, agent.id[:8])

    def update_agent(self, agent_id: str, **fields) -> bool:
        """Update specific fields on an agent record. Returns True if found."""
        with self._exclusive_lock():
            agents = self._read_unlocked()
            for d in agents:
                if d.get("id") == agent_id:
                    d.update(fields)
                    self._write_unlocked(agents)
                    return True
        return False

    def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent record. Returns True if found."""
        with self._exclusive_lock():
            agents = self._read_unlocked()
            before = len(agents)
            agents = [d for d in agents if d.get("id") != agent_id]
            if len(agents) < before:
                self._write_unlocked(agents)
                return True
        return False

    # -- low-level I/O (caller must hold appropriate lock) ------------------

    def _read_locked(self) -> list[dict]:
        """Read agents list under shared lock."""
        with self._shared_lock():
            return self._read_unlocked()

    def _read_unlocked(self) -> list[dict]:
        """Read agents list without acquiring lock (caller holds lock)."""
        if not self._agents_path.exists():
            return []
        try:
            raw = json.loads(self._agents_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except (json.JSONDecodeError, TypeError, OSError) as e:
            log.warning("Corrupt agents.json, returning empty: %s", e)
            return []

    def _write_unlocked(self, agents: list[dict]) -> None:
        """Write agents list without acquiring lock (caller holds lock)."""
        data = json.dumps(agents, indent=2).encode("utf-8")
        tmp = self._agents_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.rename(self._agents_path)

    # -- locking ------------------------------------------------------------

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

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _from_dict(d: dict) -> SubAgent:
        """Construct SubAgent from dict, ignoring unknown fields."""
        known = {f.name for f in SubAgent.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return SubAgent(**filtered)
