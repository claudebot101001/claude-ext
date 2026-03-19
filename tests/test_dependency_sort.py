"""Tests for extension dependency validation and topological sort."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from core.extension import Extension
from core.registry import Registry


class FakeExtension(Extension):
    def __init__(self, name, deps=None, soft_deps=None):
        self.name = name
        self.dependencies = deps or []
        self.soft_dependencies = soft_deps or []

    async def start(self):
        pass

    async def stop(self):
        pass


def make_registry(*exts):
    engine = MagicMock()
    engine.events = MagicMock()
    engine.session_manager = MagicMock()
    engine.session_manager._delivery_cbs = []
    engine.session_manager._pending_deliveries = []
    engine.session_manager._delivery_flush_enabled = True
    engine.session_manager._mcp_servers = {}
    engine.session_manager._mcp_tool_meta = {}
    engine.session_manager._mcp_server_tags = {}
    engine.session_manager._system_prompt_parts = []
    engine.session_manager._env_unset = []
    engine.session_manager._disallowed_tools = []
    engine.session_manager._session_customizers = []
    engine.session_manager._pre_prompt_hooks = []
    engine.session_manager._session_taggers = []
    engine.bridge = None
    engine.services = {}
    reg = Registry(engine, {})
    reg._extensions = list(exts)
    return reg


class TestDependencyValidation:
    def test_missing_hard_dependency_raises(self):
        a = FakeExtension("a", deps=["b"])
        reg = make_registry(a)
        with pytest.raises(RuntimeError, match="requires 'b'"):
            reg._validate_and_sort()

    def test_hard_dependency_present_ok(self):
        b = FakeExtension("b")
        a = FakeExtension("a", deps=["b"])
        reg = make_registry(a, b)
        reg._validate_and_sort()
        assert [e.name for e in reg._extensions] == ["b", "a"]

    def test_soft_dependency_missing_ok(self):
        a = FakeExtension("a", soft_deps=["missing"])
        reg = make_registry(a)
        reg._validate_and_sort()
        assert [e.name for e in reg._extensions] == ["a"]

    def test_soft_dependency_reorders(self):
        a = FakeExtension("a", soft_deps=["b"])
        b = FakeExtension("b")
        reg = make_registry(a, b)
        reg._validate_and_sort()
        assert [e.name for e in reg._extensions] == ["b", "a"]

    def test_no_deps_preserves_order(self):
        a = FakeExtension("a")
        b = FakeExtension("b")
        c = FakeExtension("c")
        reg = make_registry(a, b, c)
        reg._validate_and_sort()
        assert [e.name for e in reg._extensions] == ["a", "b", "c"]

    def test_diamond_dependency(self):
        d = FakeExtension("d")
        b = FakeExtension("b", deps=["d"])
        c = FakeExtension("c", deps=["d"])
        a = FakeExtension("a", deps=["b", "c"])
        reg = make_registry(a, b, c, d)
        reg._validate_and_sort()
        names = [e.name for e in reg._extensions]
        assert names.index("d") < names.index("b")
        assert names.index("d") < names.index("c")
        assert names.index("b") < names.index("a")
        assert names.index("c") < names.index("a")

    def test_cycle_does_not_crash(self):
        a = FakeExtension("a", soft_deps=["b"])
        b = FakeExtension("b", soft_deps=["a"])
        reg = make_registry(a, b)
        reg._validate_and_sort()  # Should not raise
        assert len(reg._extensions) == 2
