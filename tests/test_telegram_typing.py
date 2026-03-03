"""Tests for Telegram typing indicator functionality."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.session import SessionStatus


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(tmp_path):
    engine = MagicMock()
    engine.session_manager.base_dir = tmp_path / "sessions"
    engine.session_manager.sessions = {}
    engine.session_manager.max_sessions_per_user = 5
    engine.session_manager.get_sessions_for_user = MagicMock(return_value=[])
    engine.session_manager.create_session = AsyncMock()
    engine.session_manager.send_prompt = AsyncMock(return_value=0)
    engine.session_manager.stop_session = AsyncMock(return_value=(True, 0))
    engine.session_manager.add_delivery_callback = MagicMock()
    engine.session_manager.list_mcp_tools = MagicMock(return_value={})
    engine.pending = MagicMock()
    engine.services = {}
    engine.events = MagicMock()
    engine.registry = None
    return engine


@pytest.fixture
def ext(engine, tmp_path):
    from extensions.telegram.extension import ExtensionImpl

    ext = ExtensionImpl()
    ext.configure(
        engine, {"token": "fake-token", "allowed_users": [111], "working_dir": str(tmp_path)}
    )
    ext.app = MagicMock()
    ext.app.bot = MagicMock()
    ext.app.bot.send_message = AsyncMock()
    ext.app.bot.send_chat_action = AsyncMock()
    return ext


def _make_session(
    session_id="test-session-id",
    slot=1,
    name="session-1",
    user_id="111",
    working_dir="/tmp/test",
    status=SessionStatus.IDLE,
):
    session = MagicMock()
    session.id = session_id
    session.slot = slot
    session.name = name
    session.user_id = user_id
    session.working_dir = working_dir
    session.status = status
    session.context = {"chat_id": 999}
    session.last_prompt = None
    return session


def _make_update(user_id=111, chat_id=999):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = "hello"
    return update


class TestTypingStartsOnProcessing:
    def test_typing_starts_when_position_zero(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update()
        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_message(update, ctx)

            # Typing task should have been started
            assert "test-session-id" in ext._typing_tasks
            task = ext._typing_tasks["test-session-id"]
            assert not task.done()

            # Clean up
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(_test())

    def test_typing_not_started_when_queued(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.send_prompt = AsyncMock(return_value=1)

        update = _make_update()
        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_message(update, ctx)

            # No typing task should exist
            assert "test-session-id" not in ext._typing_tasks

        _run(_test())


class TestTypingCancelledOnDelivery:
    def test_typing_cancelled_on_stream_text(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _test():
            # Start typing
            ext._start_typing(999, "test-session-id")
            assert "test-session-id" in ext._typing_tasks

            # Deliver a stream text event
            await ext._deliver_result(
                "test-session-id",
                "hello",
                {"is_stream": True, "stream_type": "text"},
            )

            # Typing should be cancelled
            assert "test-session-id" not in ext._typing_tasks

        _run(_test())

    def test_typing_cancelled_on_final(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _test():
            ext._start_typing(999, "test-session-id")
            assert "test-session-id" in ext._typing_tasks

            await ext._deliver_result(
                "test-session-id",
                "result",
                {"is_final": True, "total_cost_usd": 0.01, "num_turns": 1},
            )

            assert "test-session-id" not in ext._typing_tasks

        _run(_test())

    def test_typing_cancelled_on_heartbeat(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _test():
            ext._start_typing(999, "test-session-id")

            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_heartbeat": True, "elapsed_s": 120},
            )

            assert "test-session-id" not in ext._typing_tasks

        _run(_test())


class TestTypingCancelledOnStop:
    def test_typing_cancelled_on_cmd_stop(self, ext):
        session = _make_session(status=SessionStatus.BUSY)
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update()
        update.message.text = "/stop"
        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            ext._start_typing(999, "test-session-id")
            assert "test-session-id" in ext._typing_tasks

            await ext._cmd_stop(update, ctx)

            assert "test-session-id" not in ext._typing_tasks

        _run(_test())


class TestTypingCancelledOnDelete:
    def test_typing_cancelled_on_cmd_delete(self, ext):
        session = _make_session(status=SessionStatus.IDLE)
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.destroy_session = AsyncMock()
        ext.engine.session_manager.get_session_by_slot = MagicMock(return_value=session)

        update = _make_update()
        update.message.text = "/delete 1"
        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            ext._start_typing(999, "test-session-id")

            await ext._cmd_delete(update, ctx)

            assert "test-session-id" not in ext._typing_tasks

        _run(_test())


class TestTypingCancelledOnExtStop:
    def test_all_typing_cancelled_on_ext_stop(self, ext):
        async def _test():
            ext._start_typing(999, "session-a")
            ext._start_typing(999, "session-b")
            assert len(ext._typing_tasks) == 2

            await ext.stop()

            assert len(ext._typing_tasks) == 0

        _run(_test())


class TestKeepTypingLoop:
    def test_keep_typing_sends_actions(self, ext):
        async def _test():
            ext._start_typing(999, "test-session-id")
            # Let it run briefly
            await asyncio.sleep(0.05)
            # Cancel it
            ext._cancel_typing("test-session-id")
            # It should have sent at least one typing action
            ext.app.bot.send_chat_action.assert_called()
            call_args = ext.app.bot.send_chat_action.call_args
            assert (
                call_args.kwargs.get("action") == "typing" or call_args[1].get("action") == "typing"
            )

        _run(_test())

    def test_cancel_typing_idempotent(self, ext):
        """Cancelling typing for non-existent session should not error."""
        ext._cancel_typing("nonexistent-session")
        assert "nonexistent-session" not in ext._typing_tasks


class TestMediaTypingIndicator:
    def test_typing_starts_on_media(self, ext, tmp_path):
        session = _make_session(working_dir=str(tmp_path))
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update()
        photo_size = MagicMock()
        photo_size.file_id = "photo-id"
        photo_size.file_unique_id = "uniq123"
        update.message.photo = [photo_size]
        update.message.document = None
        update.message.caption = None

        tg_file = MagicMock()
        tg_file.download_to_drive = AsyncMock()
        ext.app.bot.get_file = AsyncMock(return_value=tg_file)

        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_media(update, ctx)

            assert "test-session-id" in ext._typing_tasks
            # Clean up
            ext._cancel_typing("test-session-id")

        _run(_test())
