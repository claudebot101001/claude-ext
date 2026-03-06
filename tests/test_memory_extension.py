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


class TestMagmaConfig:
    """MAGMA subsystem should be disabled by default and config-gated."""

    def test_magma_disabled_by_default(self, engine):
        """No knowledge injection or reflection when magma not configured."""
        ext = ExtensionImpl()
        ext.configure(engine, {})
        _run(ext.start())
        # 2 customizers: constitution + user_profile (NOT knowledge_injection)
        assert engine.session_manager.add_session_customizer.call_count == 2
        # No delivery callback (reflection engine not registered)
        engine.session_manager.add_delivery_callback.assert_not_called()
        assert ext._magma_enabled is False

    def test_magma_enabled(self, engine):
        """When magma.enabled=true, injection + reflection are registered."""
        ext = ExtensionImpl()
        ext.configure(engine, {"magma": {"enabled": True}})
        _run(ext.start())
        # 3 customizers: constitution + user_profile + knowledge_injection
        assert engine.session_manager.add_session_customizer.call_count == 3
        engine.session_manager.add_delivery_callback.assert_called_once()
        assert ext._magma_enabled is True

    def test_magma_enabled_ki_disabled(self, engine):
        """magma enabled but knowledge_injection disabled: reflection only."""
        ext = ExtensionImpl()
        ext.configure(
            engine,
            {"magma": {"enabled": True}, "knowledge_injection": {"enabled": False}},
        )
        _run(ext.start())
        # 2 customizers (no knowledge injection), but reflection still registered
        assert engine.session_manager.add_session_customizer.call_count == 2
        engine.session_manager.add_delivery_callback.assert_called_once()

    def test_health_check_includes_magma(self, engine):
        ext = ExtensionImpl()
        ext.configure(engine, {})
        _run(ext.start())
        health = _run(ext.health_check())
        assert "magma_enabled" in health
        assert health["magma_enabled"] is False


class TestKnowledgeInjectionContextGate:
    """Knowledge injection should only fire for sessions with context.magma=True."""

    def test_regular_session_no_injection(self, engine):
        """Sessions without context.magma get no knowledge injection."""
        ext = ExtensionImpl()
        ext.configure(engine, {"magma": {"enabled": True}})
        _run(ext.start())

        session = MagicMock()
        session.id = "test-session"
        session.context = {"chat_id": 12345}

        result = ext._knowledge_injection_customizer(session)
        assert result is None

    def test_session_without_context_no_injection(self, engine):
        """Sessions with empty context get no knowledge injection."""
        ext = ExtensionImpl()
        ext.configure(engine, {"magma": {"enabled": True}})
        _run(ext.start())

        session = MagicMock()
        session.id = "test-session"
        session.context = {}

        result = ext._knowledge_injection_customizer(session)
        assert result is None

    def test_magma_session_gets_injection(self, engine):
        """Sessions with context.magma=True get knowledge injection."""
        ext = ExtensionImpl()
        ext.configure(engine, {"magma": {"enabled": True}})
        _run(ext.start())

        # Seed graph + store with a note
        ext._graph.set_meta("topics/test.md", importance=0.9)
        ext._store.write("topics/test.md", "# Test Note\nSome content here.")

        session = MagicMock()
        session.id = "audit-session"
        session.context = {"chat_id": 12345, "magma": True}

        result = ext._knowledge_injection_customizer(session)
        assert result is not None
        assert result.extra_system_prompt is not None
        assert len(result.extra_system_prompt) == 1
        assert "KNOWLEDGE CONTEXT" in result.extra_system_prompt[0]


class TestMemoryExtensionStop:
    def test_stop_removes_service(self, ext):
        _run(ext.start())
        assert "memory" in ext.engine.services
        _run(ext.stop())
        assert "memory" not in ext.engine.services

    def test_stop_without_start(self, ext):
        # Should not raise
        _run(ext.stop())
