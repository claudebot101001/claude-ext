"""Tests for registry startup rollback behavior."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.bridge import BridgeServer
from core.extension import Extension
from core.registry import Registry
from core.session import SessionManager


def _run(coro):
    return asyncio.run(coro)


class _Engine:
    def __init__(self, tmp_path: Path):
        self.events = MagicMock()
        self.services = {}
        self.session_manager = SessionManager(
            base_dir=tmp_path / "state",
            engine_config={"permission_mode": "bypassPermissions"},
        )
        self.bridge = BridgeServer(tmp_path / "bridge.sock")


class _CallbackExtension(Extension):
    def __init__(self, name: str, sink: list[tuple]):
        self.name = name
        self._sink = sink
        self.stopped = False

    async def start(self):
        async def _cb(session_id: str, text: str, metadata: dict):
            self._sink.append((self.name, session_id, text, metadata.get("is_final")))

        self.engine.session_manager.add_delivery_callback(_cb)

    async def stop(self):
        self.stopped = True


class _GoodExtension(Extension):
    name = "good"

    def __init__(self):
        self.stopped = False

    async def start(self):
        self.engine.session_manager.add_session_tagger(lambda session: {"good_tag"})
        self.engine.services["good"] = self
        self.engine.bridge.add_handler(self._handler)
        self.engine.session_manager.register_mcp_server(
            "good",
            {"command": "python", "args": ["good.py"]},
            tags=["group_a"],
        )
        self.engine.session_manager.add_system_prompt("good prompt")

    async def stop(self):
        self.stopped = True

    async def _handler(self, method: str, params: dict) -> dict | None:
        return None


class _FailingExtension(Extension):
    name = "failing"

    def __init__(self):
        self.stopped = False

    async def start(self):
        self.engine.session_manager.add_session_tagger(lambda session: {"failing_tag"})
        self.engine.services["failing"] = self
        self.engine.bridge.add_handler(self._handler)
        self.engine.session_manager.register_mcp_server(
            "failing",
            {"command": "python", "args": ["fail.py"]},
            tags=["group_b"],
        )
        self.engine.session_manager.add_system_prompt("failing prompt")
        raise RuntimeError("boom")

    async def stop(self):
        self.stopped = True

    async def _handler(self, method: str, params: dict) -> dict | None:
        return None


@pytest.fixture
def engine(tmp_path):
    return _Engine(tmp_path)


class TestRegistryStartup:
    def test_start_all_flushes_recovered_deliveries_after_all_extensions_start(self, engine):
        registry = Registry(engine, {})
        calls: list[tuple] = []
        first = _CallbackExtension("first", calls)
        second = _CallbackExtension("second", calls)
        first.configure(engine, {})
        second.configure(engine, {})
        registry._extensions = [first, second]

        async def run():
            engine.session_manager._pending_deliveries.append(
                ("sess-1", "done", {"is_final": True})
            )
            await registry.start_all()
            await asyncio.sleep(0)

        _run(run())

        assert calls == [
            ("first", "sess-1", "done", True),
            ("second", "sess-1", "done", True),
        ]
        assert engine.session_manager._pending_deliveries == []
        assert engine.session_manager._delivery_flush_enabled is True

    def test_start_all_rolls_back_runtime_state_on_failure(self, engine):
        registry = Registry(engine, {})
        baseline_service = object()

        async def base_handler(method: str, params: dict) -> dict | None:
            return None

        engine.session_manager.add_session_tagger(lambda session: {"base_tag"})
        engine.services["base"] = baseline_service
        engine.bridge.add_handler(base_handler)
        engine.session_manager.register_mcp_server(
            "base",
            {"command": "python", "args": ["base.py"]},
            tags=["baseline"],
        )
        engine.session_manager.add_system_prompt("base prompt")
        engine.session_manager._pending_deliveries.append(("sess-1", "done", {"is_final": True}))

        good = _GoodExtension()
        failing = _FailingExtension()
        good.configure(engine, {})
        failing.configure(engine, {})
        registry._extensions = [good, failing]

        with pytest.raises(RuntimeError, match="boom"):
            _run(registry.start_all())

        assert good.stopped is True
        assert failing.stopped is True
        assert engine.services == {"base": baseline_service}
        assert engine.bridge._handlers == [base_handler]
        assert engine.session_manager._mcp_servers == {
            "base": {"command": "python", "args": ["base.py"]}
        }
        assert engine.session_manager._mcp_server_tags == {"base": {"baseline"}}
        assert engine.session_manager._system_prompt_parts == [("base prompt", None)]
        assert engine.session_manager._delivery_cbs == []
        assert engine.session_manager._pending_deliveries == [
            ("sess-1", "done", {"is_final": True})
        ]
        assert engine.session_manager._delivery_flush_enabled is True
        assert len(engine.session_manager._session_taggers) == 1
        assert engine.session_manager.get_session_tags(MagicMock()) == {"base_tag"}
        engine.events.log.assert_any_call("ext.start_failed", detail={"name": "failing"})
