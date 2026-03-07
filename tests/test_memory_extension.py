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


class TestDomainConfig:
    """Domain knowledge stores should be config-gated and isolated."""

    def test_no_domains_by_default(self, engine):
        """No domain stores when magma.domains not configured."""
        ext = ExtensionImpl()
        ext.configure(engine, {})
        _run(ext.start())
        # 2 customizers: constitution + user_profile
        assert engine.session_manager.add_session_customizer.call_count == 2
        engine.session_manager.add_delivery_callback.assert_not_called()
        assert ext._domain_manager is None

    def test_domain_with_injection_and_reflection(self, engine):
        """Domain with injection + reflection registers all customizers."""
        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "magma": {
                    "domains": {
                        "vuln": {
                            "description": "test",
                            "knowledge_injection": {"enabled": True},
                            "reflection": {"llm_enabled": True},
                        }
                    }
                }
            },
        )
        _run(ext.start())
        # 4 customizers: constitution + user_profile + domain_scoping + knowledge_injection
        assert engine.session_manager.add_session_customizer.call_count == 4
        engine.session_manager.add_delivery_callback.assert_called_once()
        assert ext._domain_manager is not None
        assert "vuln" in ext._domain_manager.list_domains()

    def test_domain_without_injection(self, engine):
        """Domain without injection enabled: domain_scoping only, no injection customizer."""
        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "magma": {
                    "domains": {
                        "vuln": {
                            "description": "test",
                            "knowledge_injection": {"enabled": False},
                            "reflection": {"llm_enabled": False},
                        }
                    }
                }
            },
        )
        _run(ext.start())
        # 3 customizers: constitution + user_profile + domain_scoping
        assert engine.session_manager.add_session_customizer.call_count == 3
        engine.session_manager.add_delivery_callback.assert_not_called()

    def test_health_check_includes_domains(self, engine):
        ext = ExtensionImpl()
        ext.configure(engine, {})
        _run(ext.start())
        health = _run(ext.health_check())
        assert "domains" in health
        assert health["domains"] == []


class TestKnowledgeInjectionContextGate:
    """Knowledge injection should only fire for sessions with context.domains."""

    @pytest.fixture
    def domain_ext(self, engine):
        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "magma": {
                    "domains": {
                        "vuln": {
                            "description": "test vuln patterns",
                            "knowledge_injection": {
                                "enabled": True,
                                "max_chars": 8000,
                                "max_notes": 10,
                            },
                        }
                    }
                }
            },
        )
        _run(ext.start())
        return ext

    def test_regular_session_no_injection(self, domain_ext):
        """Sessions without context.domains get no knowledge injection."""
        session = MagicMock()
        session.id = "test-session"
        session.context = {"chat_id": 12345}

        result = domain_ext._knowledge_injection_customizer(session)
        assert result is None

    def test_session_without_context_no_injection(self, domain_ext):
        """Sessions with empty context get no knowledge injection."""
        session = MagicMock()
        session.id = "test-session"
        session.context = {}

        result = domain_ext._knowledge_injection_customizer(session)
        assert result is None

    def test_domain_session_gets_injection(self, domain_ext):
        """Sessions with context.domains=["vuln"] get knowledge injection."""
        # Seed domain graph + store with a note
        domain_store = domain_ext._domain_manager.get_store("vuln")
        domain_graph = domain_ext._domain_manager.get_graph("vuln")
        domain_graph.set_meta("topics/test.md", importance=0.9)
        domain_store.write("topics/test.md", "# Test Note\nSome content here.")

        session = MagicMock()
        session.id = "audit-session"
        session.context = {"chat_id": 12345, "domains": ["vuln"]}

        result = domain_ext._knowledge_injection_customizer(session)
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
