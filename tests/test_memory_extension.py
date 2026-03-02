"""Tests for memory extension lifecycle."""

import asyncio
from unittest.mock import MagicMock

import pytest

from extensions.memory.extension import ExtensionImpl


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture
def memory_dir(tmp_path):
    return tmp_path / "sessions" / "memory"


@pytest.fixture
def engine(tmp_path):
    """Minimal mock engine with session_manager."""
    engine = MagicMock()
    engine.session_manager.base_dir = tmp_path / "sessions"
    engine.services = {}
    return engine


@pytest.fixture
def ext(engine):
    ext = ExtensionImpl()
    ext.configure(engine, {})
    return ext


class TestMemoryExtensionStart:
    def test_start_creates_memory_dir(self, ext, memory_dir):
        _run(ext.start())
        assert memory_dir.exists()

    def test_start_creates_seed_topics_index(self, ext, memory_dir):
        _run(ext.start())
        topics_file = memory_dir / "TOPICS_INDEX.md"
        assert topics_file.exists()
        content = topics_file.read_text(encoding="utf-8")
        assert "Topics Index" in content

    def test_start_does_not_overwrite_existing_topics_index(self, ext, memory_dir):
        memory_dir.mkdir(parents=True)
        existing = "# My existing index\nDo not overwrite!"
        (memory_dir / "TOPICS_INDEX.md").write_text(existing, encoding="utf-8")

        _run(ext.start())

        content = (memory_dir / "TOPICS_INDEX.md").read_text(encoding="utf-8")
        assert content == existing

    def test_start_registers_mcp_server(self, ext):
        _run(ext.start())
        ext.engine.session_manager.register_mcp_server.assert_called_once()
        call_args = ext.engine.session_manager.register_mcp_server.call_args
        assert call_args[0][0] == "memory"
        config = call_args[0][1]
        assert "command" in config
        assert "MEMORY_DIR" in config["env"]

    def test_start_registers_system_prompt(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_system_prompt.assert_called_once()
        prompt = ext.engine.session_manager.add_system_prompt.call_args[0][0]
        assert "memory_search" in prompt
        assert "personality_read" in prompt
        assert "SESSION START" in prompt

    def test_start_registers_service(self, ext):
        _run(ext.start())
        assert "memory" in ext.engine.services
        # Service should be a MemoryStore instance
        from extensions.memory.store import MemoryStore

        assert isinstance(ext.engine.services["memory"], MemoryStore)


class TestMemoryExtensionStop:
    def test_stop_removes_service(self, ext):
        _run(ext.start())
        assert "memory" in ext.engine.services
        _run(ext.stop())
        assert "memory" not in ext.engine.services

    def test_stop_without_start(self, ext):
        # Should not raise
        _run(ext.stop())
