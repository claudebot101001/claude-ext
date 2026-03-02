"""Tests for session_ask extension: bridge handlers, delivery callback, lifecycle."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.pending import PendingStore
from core.session import SessionStatus
from extensions.session_ask.extension import ExtensionImpl


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture
def engine():
    """Minimal mock engine with session_manager and pending store."""
    engine = MagicMock()
    sm = engine.session_manager
    sm.sessions = {}
    sm.send_prompt = AsyncMock(return_value=0)
    sm.deliver = AsyncMock()
    sm.get_sessions_for_user = MagicMock(return_value=[])

    engine.services = {}
    engine.events = MagicMock()
    engine.bridge = MagicMock()
    engine.pending = PendingStore()
    return engine


@pytest.fixture
def config():
    return {"timeout": 5, "max_question_length": 100}


@pytest.fixture
def ext(engine, config):
    ext = ExtensionImpl()
    ext.configure(engine, config)
    return ext


def _mock_session(
    session_id="sess-A",
    user_id="user-1",
    name="session-A",
    slot=1,
    status=SessionStatus.IDLE,
):
    s = MagicMock()
    s.id = session_id
    s.user_id = user_id
    s.name = name
    s.slot = slot
    s.status = status
    return s


# -- Lifecycle ----------------------------------------------------------------


class TestLifecycle:
    def test_start_registers_services(self, ext, engine):
        _run(ext.start())
        assert "session_ask" in engine.services
        engine.session_manager.register_mcp_server.assert_called_once()
        engine.bridge.add_handler.assert_called_once()
        engine.session_manager.add_system_prompt.assert_called_once()
        engine.session_manager.add_delivery_callback.assert_called_once()

    def test_stop_removes_services(self, ext, engine):
        _run(ext.start())
        _run(ext.stop())
        assert "session_ask" not in engine.services

    def test_stop_cancels_active_asks(self, ext, engine):
        async def _test():
            await ext.start()
            # Simulate an active ask (needs running loop for Future creation)
            entry = engine.pending.register(session_id="sess-A", data={"type": "session_ask"})
            ext._active_asks[entry.key] = "sess-A"

            await ext.stop()
            assert len(ext._active_asks) == 0
            # The pending entry should be resolved
            assert entry.future.done()

        _run(_test())

    def test_health_check(self, ext):
        result = _run(ext.health_check())
        assert result["status"] == "ok"
        assert result["active_asks"] == 0


# -- session_ask handler ------------------------------------------------------


class TestHandleAsk:
    def _setup_sessions(self, engine):
        sess_a = _mock_session("sess-A", "user-1", "session-A", 1)
        sess_b = _mock_session("sess-B", "user-1", "session-B", 2)
        engine.session_manager.sessions = {"sess-A": sess_a, "sess-B": sess_b}
        return sess_a, sess_b

    def test_ask_missing_question(self, ext, engine):
        result = _run(ext._handle_ask({"session_id": "sess-A", "target_session_id": "sess-B"}))
        assert "error" in result
        assert "question is required" in result["error"]

    def test_ask_missing_target(self, ext, engine):
        result = _run(ext._handle_ask({"session_id": "sess-A", "question": "hello?"}))
        assert "error" in result
        assert "target_session_id is required" in result["error"]

    def test_ask_self(self, ext, engine):
        self._setup_sessions(engine)
        result = _run(
            ext._handle_ask(
                {"session_id": "sess-A", "target_session_id": "sess-A", "question": "hello?"}
            )
        )
        assert "error" in result
        assert "Cannot ask yourself" in result["error"]

    def test_ask_target_not_found(self, ext, engine):
        engine.session_manager.sessions = {
            "sess-A": _mock_session("sess-A", "user-1", "session-A")
        }
        result = _run(
            ext._handle_ask(
                {
                    "session_id": "sess-A",
                    "target_session_id": "nonexistent",
                    "question": "hello?",
                }
            )
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_ask_asking_session_not_found(self, ext, engine):
        engine.session_manager.sessions = {}
        result = _run(
            ext._handle_ask(
                {"session_id": "sess-A", "target_session_id": "sess-B", "question": "hello?"}
            )
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_ask_different_user(self, ext, engine):
        sess_a = _mock_session("sess-A", "user-1", "session-A")
        sess_b = _mock_session("sess-B", "user-2", "session-B")
        engine.session_manager.sessions = {"sess-A": sess_a, "sess-B": sess_b}
        result = _run(
            ext._handle_ask(
                {"session_id": "sess-A", "target_session_id": "sess-B", "question": "hello?"}
            )
        )
        assert "error" in result
        assert "different user" in result["error"]

    def test_ask_dead_target(self, ext, engine):
        sess_a = _mock_session("sess-A", "user-1", "session-A")
        sess_b = _mock_session("sess-B", "user-1", "session-B", status=SessionStatus.DEAD)
        engine.session_manager.sessions = {"sess-A": sess_a, "sess-B": sess_b}
        result = _run(
            ext._handle_ask(
                {"session_id": "sess-A", "target_session_id": "sess-B", "question": "hello?"}
            )
        )
        assert "error" in result
        assert "dead" in result["error"]

    def test_ask_send_prompt_failure_cleans_up(self, ext, engine):
        """TOCTOU: send_prompt fails after pending.register — must clean up."""
        self._setup_sessions(engine)
        engine.session_manager.send_prompt = AsyncMock(side_effect=KeyError("sess-B"))

        result = _run(
            ext._handle_ask(
                {"session_id": "sess-A", "target_session_id": "sess-B", "question": "hello?"}
            )
        )
        assert "error" in result
        assert "Failed to deliver" in result["error"]
        # Verify no orphaned pending entries
        assert len(ext._active_asks) == 0

    def test_ask_truncates_long_question(self, ext, engine):
        self._setup_sessions(engine)
        long_question = "x" * 200  # max_question_length = 100

        async def _test():
            # Resolve the pending entry from a background task
            async def _resolve_soon():
                await asyncio.sleep(0.1)
                # Find the pending entry and resolve it
                for key in list(ext._active_asks):
                    engine.pending.resolve(key, "answer")

            asyncio.create_task(_resolve_soon())
            return await ext._handle_ask(
                {
                    "session_id": "sess-A",
                    "target_session_id": "sess-B",
                    "question": long_question,
                }
            )

        result = _run(_test())
        # Verify the prompt was sent with truncated question
        call_args = engine.session_manager.send_prompt.call_args
        prompt_text = call_args[0][1]
        assert "[truncated]" in prompt_text

    def test_ask_success_flow(self, ext, engine):
        """End-to-end: ask → resolve → reply returned."""
        self._setup_sessions(engine)

        async def _test():
            async def _resolve_soon():
                await asyncio.sleep(0.1)
                for key in list(ext._active_asks):
                    engine.pending.resolve(key, "the answer is 42")

            asyncio.create_task(_resolve_soon())
            return await ext._handle_ask(
                {
                    "session_id": "sess-A",
                    "target_session_id": "sess-B",
                    "question": "What is the answer?",
                }
            )

        result = _run(_test())
        assert result["reply"] == "the answer is 42"
        assert len(ext._active_asks) == 0
        # Verify send_prompt was called with correct target
        engine.session_manager.send_prompt.assert_called_once()
        call_args = engine.session_manager.send_prompt.call_args[0]
        assert call_args[0] == "sess-B"
        assert "Inter-Session Request" in call_args[1]
        assert "What is the answer?" in call_args[1]

    def test_ask_timeout(self, ext, engine):
        """Ask times out when target doesn't reply."""
        self._setup_sessions(engine)
        ext._timeout = 0.2  # Very short timeout

        result = _run(
            ext._handle_ask(
                {"session_id": "sess-A", "target_session_id": "sess-B", "question": "hello?"}
            )
        )
        assert result.get("timed_out") is True
        assert len(ext._active_asks) == 0


# -- session_reply handler ----------------------------------------------------


class TestHandleReply:
    def test_reply_missing_request_id(self, ext, engine):
        result = _run(ext._handle_reply({"session_id": "sess-B", "reply": "answer"}))
        assert "error" in result
        assert "request_id is required" in result["error"]

    def test_reply_missing_reply(self, ext, engine):
        result = _run(ext._handle_reply({"session_id": "sess-B", "request_id": "abc123"}))
        assert "error" in result
        assert "reply is required" in result["error"]

    def test_reply_nonexistent_request(self, ext, engine):
        result = _run(
            ext._handle_reply(
                {"session_id": "sess-B", "request_id": "nonexistent", "reply": "answer"}
            )
        )
        assert "error" in result
        assert "No pending request" in result["error"]

    def test_reply_wrong_type(self, ext, engine):
        """Reject reply to a non-session_ask pending entry."""

        async def _test():
            entry = engine.pending.register(session_id="sess-A", data={"type": "ask_user"})
            return await ext._handle_reply(
                {"session_id": "sess-B", "request_id": entry.key, "reply": "answer"}
            )

        result = _run(_test())
        assert "error" in result
        assert "does not correspond" in result["error"]

    def test_reply_wrong_session(self, ext, engine):
        """Only the intended target can reply."""

        async def _test():
            entry = engine.pending.register(
                session_id="sess-A",
                data={"type": "session_ask", "target_session_id": "sess-B"},
            )
            result = await ext._handle_reply(
                {"session_id": "sess-C", "request_id": entry.key, "reply": "answer"}
            )
            return result

        result = _run(_test())
        assert "error" in result
        assert "directed to session" in result["error"]

    def test_reply_success(self, ext, engine):
        """Successful reply resolves the pending entry."""

        async def _test():
            entry = engine.pending.register(
                session_id="sess-A",
                data={"type": "session_ask", "target_session_id": "sess-B"},
            )
            result = await ext._handle_reply(
                {"session_id": "sess-B", "request_id": entry.key, "reply": "the answer"}
            )
            return result, entry

        result, entry = _run(_test())
        assert result["resolved"] is True
        assert entry.future.done()
        assert entry.future.result() == "the answer"

    def test_reply_already_resolved(self, ext, engine):
        """Reply to already-resolved request returns error."""

        async def _test():
            entry = engine.pending.register(
                session_id="sess-A",
                data={"type": "session_ask", "target_session_id": "sess-B"},
            )
            engine.pending.resolve(entry.key, "first answer")
            return await ext._handle_reply(
                {"session_id": "sess-B", "request_id": entry.key, "reply": "second answer"}
            )

        result = _run(_test())
        assert "error" in result
        assert "already resolved" in result["error"]


# -- session_list handler -----------------------------------------------------


class TestHandleList:
    def test_list_session_not_found(self, ext, engine):
        engine.session_manager.sessions = {}
        result = _run(ext._handle_list({"session_id": "nonexistent"}))
        assert "error" in result

    def test_list_returns_sessions(self, ext, engine):
        sess_a = _mock_session("sess-A", "user-1", "session-A", 1)
        sess_b = _mock_session("sess-B", "user-1", "session-B", 2)
        engine.session_manager.sessions = {"sess-A": sess_a, "sess-B": sess_b}
        engine.session_manager.get_sessions_for_user.return_value = [sess_a, sess_b]

        result = _run(ext._handle_list({"session_id": "sess-A"}))
        sessions = result["sessions"]
        assert len(sessions) == 2

        # Check is_self flag
        self_sessions = [s for s in sessions if s["is_self"]]
        assert len(self_sessions) == 1
        assert self_sessions[0]["session_id"] == "sess-A"


# -- Delivery callback --------------------------------------------------------


class TestOnDelivery:
    def test_non_final_ignored(self, ext, engine):
        """Non-final deliveries are ignored."""
        ext._active_asks["key1"] = "sess-A"
        _run(ext._on_delivery("sess-A", "text", {"is_final": False}))
        assert "key1" in ext._active_asks

    def test_final_non_stopped_ignored(self, ext, engine):
        """Final but not stopped/error deliveries are ignored."""
        ext._active_asks["key1"] = "sess-A"
        _run(ext._on_delivery("sess-A", "text", {"is_final": True}))
        assert "key1" in ext._active_asks

    def test_asking_session_stopped_cancels_pending(self, ext, engine):
        """When asking session A is stopped, its pending ask is cancelled."""

        async def _test():
            entry = engine.pending.register(
                session_id="sess-A",
                data={"type": "session_ask", "target_session_id": "sess-B"},
            )
            ext._active_asks[entry.key] = "sess-A"

            await ext._on_delivery("sess-A", "stopped", {"is_final": True, "is_stopped": True})
            assert entry.key not in ext._active_asks
            assert entry.future.done()

        _run(_test())

    def test_target_session_stopped_cancels_pending(self, ext, engine):
        """When target session B is stopped, asker A's pending is cancelled."""

        async def _test():
            entry = engine.pending.register(
                session_id="sess-A",
                data={"type": "session_ask", "target_session_id": "sess-B"},
            )
            ext._active_asks[entry.key] = "sess-A"

            await ext._on_delivery("sess-B", "error", {"is_final": True, "is_error": True})
            assert entry.key not in ext._active_asks
            assert entry.future.done()
            result = entry.future.result()
            assert "Target session" in result["error"]

        _run(_test())

    def test_unrelated_session_ignored(self, ext, engine):
        """Delivery for unrelated session doesn't affect pending asks."""

        async def _test():
            entry = engine.pending.register(
                session_id="sess-A",
                data={"type": "session_ask", "target_session_id": "sess-B"},
            )
            ext._active_asks[entry.key] = "sess-A"

            await ext._on_delivery(
                "sess-C", "stopped", {"is_final": True, "is_stopped": True}
            )
            assert entry.key in ext._active_asks
            assert not entry.future.done()

        _run(_test())


# -- Bridge handler dispatch --------------------------------------------------


class TestBridgeHandler:
    def test_unknown_method_returns_none(self, ext):
        result = _run(ext._bridge_handler("unknown_method", {}))
        assert result is None

    def test_dispatches_session_ask(self, ext, engine):
        engine.session_manager.sessions = {}
        result = _run(
            ext._bridge_handler(
                "session_ask",
                {"session_id": "sess-A", "target_session_id": "sess-B", "question": "hi"},
            )
        )
        assert "error" in result  # sess-A not found, but method was dispatched

    def test_dispatches_session_reply(self, ext):
        result = _run(
            ext._bridge_handler(
                "session_reply",
                {"session_id": "sess-B", "request_id": "fake", "reply": "answer"},
            )
        )
        assert "error" in result  # no pending entry, but method was dispatched

    def test_dispatches_session_list(self, ext, engine):
        engine.session_manager.sessions = {}
        result = _run(
            ext._bridge_handler("session_list", {"session_id": "nonexistent"})
        )
        assert "error" in result  # session not found, but method was dispatched
