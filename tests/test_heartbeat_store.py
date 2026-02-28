"""Tests for extensions/heartbeat/store.py — HeartbeatStore."""

import json

import pytest

from extensions.heartbeat.store import HeartbeatState, HeartbeatStore


@pytest.fixture
def heartbeat_dir(tmp_path):
    return tmp_path / "heartbeat"


@pytest.fixture
def store(heartbeat_dir):
    return HeartbeatStore(heartbeat_dir)


# -- State: defaults --------------------------------------------------------


class TestHeartbeatStateDefaults:
    def test_default_state_values(self):
        state = HeartbeatState()
        assert state.enabled is True
        assert state.last_run is None
        assert state.next_run is None
        assert state.run_count == 0
        assert state.runs_today == 0
        assert state.runs_today_date is None
        assert state.consecutive_noop == 0
        assert state.active_session_id is None

    def test_load_state_returns_defaults_when_no_file(self, store):
        state = store.load_state()
        assert state.enabled is True
        assert state.run_count == 0


# -- State: save + load roundtrip -------------------------------------------


class TestHeartbeatStateSaveLoad:
    def test_save_and_load(self, store):
        state = HeartbeatState(
            enabled=False,
            last_run="2025-01-01T00:00:00+00:00",
            run_count=42,
            consecutive_noop=5,
        )
        store.save_state(state)
        loaded = store.load_state()
        assert loaded.enabled is False
        assert loaded.last_run == "2025-01-01T00:00:00+00:00"
        assert loaded.run_count == 42
        assert loaded.consecutive_noop == 5

    def test_update_state_partial(self, store):
        store.save_state(HeartbeatState(run_count=10, enabled=True))
        updated = store.update_state(run_count=11, consecutive_noop=3)
        assert updated.run_count == 11
        assert updated.consecutive_noop == 3
        assert updated.enabled is True  # unchanged

    def test_update_state_returns_updated(self, store):
        result = store.update_state(enabled=False)
        assert result.enabled is False
        # Verify persisted
        loaded = store.load_state()
        assert loaded.enabled is False

    def test_unknown_fields_ignored(self, store, heartbeat_dir):
        """Unknown fields in state.json should not crash loading."""
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        state_path = heartbeat_dir / "state.json"
        data = {"enabled": True, "run_count": 5, "future_field": "ignored"}
        state_path.write_text(json.dumps(data))
        state = store.load_state()
        assert state.run_count == 5
        assert not hasattr(state, "future_field")

    def test_corrupt_file_returns_defaults(self, store, heartbeat_dir):
        """Corrupted state.json should fall back to defaults."""
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        state_path = heartbeat_dir / "state.json"
        state_path.write_text("NOT VALID JSON {{{")
        state = store.load_state()
        assert state.enabled is True
        assert state.run_count == 0


# -- Instructions I/O -------------------------------------------------------


class TestHeartbeatInstructions:
    def test_read_none_when_no_file(self, store):
        assert store.read_instructions() is None

    def test_write_and_read(self, store):
        store.write_instructions("# Check deployments")
        content = store.read_instructions()
        assert content == "# Check deployments"

    def test_overwrite(self, store):
        store.write_instructions("version 1")
        store.write_instructions("version 2")
        assert store.read_instructions() == "version 2"

    def test_unicode_content(self, store):
        text = "# 定时检查\n日本語テスト"
        store.write_instructions(text)
        assert store.read_instructions() == text

    def test_write_returns_byte_count(self, store):
        nbytes = store.write_instructions("hello")
        assert nbytes == 5


# -- Init / directory creation -----------------------------------------------


class TestHeartbeatStoreInit:
    def test_creates_directory(self, heartbeat_dir):
        assert not heartbeat_dir.exists()
        HeartbeatStore(heartbeat_dir)
        assert heartbeat_dir.exists()

    def test_existing_directory_ok(self, heartbeat_dir):
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        assert store.heartbeat_dir == heartbeat_dir
