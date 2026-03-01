"""Tests for extensions/subagent/store.py — SubAgentStore."""

import json

import pytest

from extensions.subagent.store import SubAgent, SubAgentStore


@pytest.fixture
def subagent_dir(tmp_path):
    return tmp_path / "subagent"


@pytest.fixture
def store(subagent_dir):
    return SubAgentStore(subagent_dir)


def _make_agent(**overrides) -> SubAgent:
    """Create a SubAgent with sensible defaults."""
    defaults = {
        "id": "agent-001",
        "parent_session_id": "parent-001",
        "name": "test-worker",
        "task": "Do something",
        "paradigm": "coder",
        "user_id": "user-123",
        "working_dir": "/tmp/work",
        "status": "running",
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    defaults.update(overrides)
    return SubAgent(**defaults)


# -- Init / directory creation -----------------------------------------------


class TestStoreInit:
    def test_creates_directory(self, subagent_dir):
        assert not subagent_dir.exists()
        SubAgentStore(subagent_dir)
        assert subagent_dir.exists()

    def test_existing_directory_ok(self, subagent_dir):
        subagent_dir.mkdir(parents=True)
        store = SubAgentStore(subagent_dir)
        assert store.subagent_dir == subagent_dir


# -- Empty store -------------------------------------------------------------


class TestEmptyStore:
    def test_list_agents_empty(self, store):
        assert store.list_agents() == []

    def test_get_agent_none(self, store):
        assert store.get_agent("nonexistent") is None

    def test_delete_nonexistent(self, store):
        assert store.delete_agent("nonexistent") is False

    def test_update_nonexistent(self, store):
        assert store.update_agent("nonexistent", status="completed") is False


# -- CRUD roundtrip ----------------------------------------------------------


class TestCRUD:
    def test_add_and_get(self, store):
        agent = _make_agent()
        store.add_agent(agent)
        loaded = store.get_agent("agent-001")
        assert loaded is not None
        assert loaded.id == "agent-001"
        assert loaded.name == "test-worker"
        assert loaded.status == "running"

    def test_add_and_list(self, store):
        store.add_agent(_make_agent(id="a1", name="worker-1"))
        store.add_agent(_make_agent(id="a2", name="worker-2"))
        agents = store.list_agents()
        assert len(agents) == 2
        names = {a.name for a in agents}
        assert names == {"worker-1", "worker-2"}

    def test_list_by_parent(self, store):
        store.add_agent(_make_agent(id="a1", parent_session_id="p1"))
        store.add_agent(_make_agent(id="a2", parent_session_id="p2"))
        store.add_agent(_make_agent(id="a3", parent_session_id="p1"))
        p1_agents = store.list_agents(parent_session_id="p1")
        assert len(p1_agents) == 2
        assert {a.id for a in p1_agents} == {"a1", "a3"}

    def test_update_agent(self, store):
        store.add_agent(_make_agent())
        ok = store.update_agent("agent-001", status="completed", cost_usd=0.05)
        assert ok is True
        loaded = store.get_agent("agent-001")
        assert loaded.status == "completed"
        assert loaded.cost_usd == 0.05
        # Unchanged fields preserved
        assert loaded.name == "test-worker"

    def test_delete_agent(self, store):
        store.add_agent(_make_agent(id="a1"))
        store.add_agent(_make_agent(id="a2"))
        ok = store.delete_agent("a1")
        assert ok is True
        assert store.get_agent("a1") is None
        assert store.get_agent("a2") is not None
        assert len(store.list_agents()) == 1


# -- Data integrity ----------------------------------------------------------


class TestDataIntegrity:
    def test_unknown_fields_ignored(self, store, subagent_dir):
        """Unknown fields in agents.json should not crash loading."""
        subagent_dir.mkdir(parents=True, exist_ok=True)
        agents_path = subagent_dir / "agents.json"
        data = [
            {
                "id": "a1",
                "parent_session_id": "p1",
                "name": "w1",
                "task": "t1",
                "paradigm": "coder",
                "user_id": "u1",
                "working_dir": "/tmp",
                "future_field": "ignored",
            }
        ]
        agents_path.write_text(json.dumps(data))
        agents = store.list_agents()
        assert len(agents) == 1
        assert not hasattr(agents[0], "future_field")

    def test_corrupt_file_returns_empty(self, store, subagent_dir):
        """Corrupted agents.json should fall back to empty list."""
        subagent_dir.mkdir(parents=True, exist_ok=True)
        agents_path = subagent_dir / "agents.json"
        agents_path.write_text("NOT VALID JSON {{{")
        agents = store.list_agents()
        assert agents == []

    def test_roundtrip_all_fields(self, store):
        """All SubAgent fields should survive a save/load cycle."""
        agent = SubAgent(
            id="full-test",
            parent_session_id="parent-x",
            name="full-worker",
            task="complex task",
            paradigm="reviewer",
            user_id="user-456",
            working_dir="/tmp/work",
            worktree_enabled=True,
            worktree_path="/tmp/wt",
            worktree_branch="subagent/test-abc123",
            parent_branch="main",
            status="completed",
            created_at="2025-01-01T00:00:00+00:00",
            completed_at="2025-01-01T01:00:00+00:00",
            result_summary="All done",
            cost_usd=0.123,
            error=None,
        )
        store.add_agent(agent)
        loaded = store.get_agent("full-test")
        assert loaded.worktree_enabled is True
        assert loaded.worktree_path == "/tmp/wt"
        assert loaded.worktree_branch == "subagent/test-abc123"
        assert loaded.parent_branch == "main"
        assert loaded.status == "completed"
        assert loaded.completed_at == "2025-01-01T01:00:00+00:00"
        assert loaded.result_summary == "All done"
        assert loaded.cost_usd == 0.123
        assert loaded.error is None


# -- Defaults ----------------------------------------------------------------


class TestSubAgentDefaults:
    def test_default_values(self):
        agent = SubAgent(
            id="x",
            parent_session_id="p",
            name="n",
            task="t",
            paradigm="coder",
            user_id="u",
            working_dir="/tmp",
        )
        assert agent.worktree_enabled is False
        assert agent.worktree_path is None
        assert agent.worktree_branch is None
        assert agent.parent_branch is None
        assert agent.status == "pending"
        assert agent.created_at == ""
        assert agent.completed_at is None
        assert agent.result_summary is None
        assert agent.cost_usd is None
        assert agent.error is None
