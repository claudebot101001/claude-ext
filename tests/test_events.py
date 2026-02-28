"""Tests for core/events.py — structured event log."""

import json
import os
import threading
from pathlib import Path

import pytest

from core.events import EventLog


@pytest.fixture
def event_log(tmp_path):
    return EventLog(tmp_path / "events.jsonl")


class TestLog:
    def test_basic_log(self, event_log):
        event_log.log("session.created", "sid-1", {"slot": 1})
        lines = event_log.path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "session.created"
        assert entry["session_id"] == "sid-1"
        assert entry["detail"]["slot"] == 1
        assert "ts" in entry

    def test_multiple_logs(self, event_log):
        event_log.log("a.one")
        event_log.log("a.two")
        event_log.log("a.three")
        lines = event_log.path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_log_without_session_id(self, event_log):
        event_log.log("ext.started", detail={"name": "vault"})
        entry = json.loads(event_log.path.read_text().strip())
        assert entry["session_id"] is None
        assert entry["detail"]["name"] == "vault"

    def test_log_without_detail(self, event_log):
        event_log.log("simple.event", "s1")
        entry = json.loads(event_log.path.read_text().strip())
        assert entry["detail"] == {}

    def test_log_creates_parent_dirs(self, tmp_path):
        el = EventLog(tmp_path / "deep" / "nested" / "events.jsonl")
        el.log("test.event")
        assert el.path.exists()

    def test_log_swallows_os_error(self, event_log):
        """log() should not raise even on write failure."""
        # Make the parent directory read-only
        event_log.path.parent.chmod(0o444)
        try:
            event_log.log("should.not.raise")  # must not raise
        finally:
            event_log.path.parent.chmod(0o755)

    def test_log_is_valid_jsonl(self, event_log):
        for i in range(10):
            event_log.log(f"type.{i}", f"s{i}", {"i": i})
        for line in event_log.path.read_text().strip().splitlines():
            entry = json.loads(line)
            assert "ts" in entry
            assert "type" in entry


class TestQuery:
    def test_query_all(self, event_log):
        event_log.log("a.one", "s1")
        event_log.log("a.two", "s2")
        results = event_log.query()
        assert len(results) == 2
        # newest first
        assert results[0]["type"] == "a.two"

    def test_query_by_event_type(self, event_log):
        event_log.log("session.created", "s1")
        event_log.log("session.stopped", "s1")
        event_log.log("session.created", "s2")
        results = event_log.query(event_type="session.created")
        assert len(results) == 2
        assert all(r["type"] == "session.created" for r in results)

    def test_query_by_session_id(self, event_log):
        event_log.log("a", "s1")
        event_log.log("b", "s2")
        event_log.log("c", "s1")
        results = event_log.query(session_id="s1")
        assert len(results) == 2

    def test_query_by_type_and_session(self, event_log):
        event_log.log("session.created", "s1")
        event_log.log("session.created", "s2")
        event_log.log("session.stopped", "s1")
        results = event_log.query(event_type="session.created", session_id="s1")
        assert len(results) == 1

    def test_query_with_limit(self, event_log):
        for i in range(20):
            event_log.log("bulk", f"s{i}")
        results = event_log.query(limit=5)
        assert len(results) == 5

    def test_query_empty_file(self, event_log):
        results = event_log.query()
        assert results == []

    def test_query_nonexistent_file(self, tmp_path):
        el = EventLog(tmp_path / "nope.jsonl")
        assert el.query() == []

    def test_query_with_since(self, event_log):
        event_log.log("early", "s1")
        # Read the timestamp of the first event
        first_ts = json.loads(event_log.path.read_text().strip())["ts"]
        event_log.log("later", "s1")
        # Query since the first timestamp — should include both
        results = event_log.query(since=first_ts)
        assert len(results) == 2

    def test_query_skips_malformed_lines(self, event_log):
        event_log.log("good.event", "s1")
        with open(event_log.path, "a") as f:
            f.write("this is not json\n")
        event_log.log("another.good", "s2")
        results = event_log.query()
        assert len(results) == 2


class TestRotation:
    def test_rotation_on_size(self, tmp_path):
        el = EventLog(tmp_path / "events.jsonl")
        # Write enough to exceed 10 MB
        big_detail = {"data": "x" * 10000}
        for _ in range(1100):
            el.log("big.event", detail=big_detail)

        rotated = el.path.with_suffix(".jsonl.1")
        assert rotated.exists()
        # Original file should still exist (new writes after rotation)
        assert el.path.exists()
        # Rotated file should be big
        assert rotated.stat().st_size > 0


class TestConcurrency:
    def test_concurrent_writes(self, event_log):
        """Multiple threads writing simultaneously should not corrupt the file."""
        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    event_log.log(f"thread.{thread_id}", detail={"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        lines = event_log.path.read_text().strip().splitlines()
        assert len(lines) == 200  # 4 threads * 50 events
        for line in lines:
            json.loads(line)  # all should be valid JSON
