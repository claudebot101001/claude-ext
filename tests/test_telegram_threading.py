"""Tests for Telegram reply threading (Phase B1)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from core.session import SessionStatus
from telegram import ReplyParameters


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
        engine,
        {"token": "fake-token", "allowed_users": [111], "working_dir": str(tmp_path)},
    )
    ext.app = MagicMock()
    ext.app.bot = MagicMock()
    ext.app.bot.send_message = AsyncMock()
    return ext


def _make_session(
    slot=1,
    name="session-1",
    user_id="111",
    working_dir="/tmp/test",
    status=SessionStatus.IDLE,
    session_id="test-session-id",
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


def _make_update(user_id=111, chat_id=999, message_id=42):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = "Hello"
    update.message.message_id = message_id
    return update


class TestPromptMessageIdStored:
    """After send_prompt, the user's message_id is stored."""

    def test_handle_message_stores_prompt_id(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update(message_id=42)
        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_message(update, ctx)
            assert ext._prompt_message_ids["test-session-id"] == 42

        _run(_test())

    def test_handle_media_stores_prompt_id(self, ext, tmp_path):
        session = _make_session(working_dir=str(tmp_path))
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update(message_id=55)
        photo_size = MagicMock()
        photo_size.file_id = "photo-id"
        photo_size.file_unique_id = "uniq"
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
            assert ext._prompt_message_ids["test-session-id"] == 55

        _run(_test())


class TestReplyThreadingOnFirstFlush:
    """First stream flush uses reply_to_message_id, subsequent flushes don't."""

    def test_first_flush_threads_to_prompt(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext._prompt_message_ids["test-session-id"] = 42

        from extensions.telegram.extension import _StreamBuffer

        buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
        buf.text_parts = ["Hello world"]
        ext._stream_buffers["test-session-id"] = buf

        async def _test():
            await ext._flush_stream_buffer("test-session-id")

            # Verify send_message was called with reply_parameters
            send_call = ext.app.bot.send_message.call_args
            assert send_call.kwargs.get("reply_parameters") is not None
            rp = send_call.kwargs["reply_parameters"]
            assert rp.message_id == 42
            assert rp.allow_sending_without_reply is True
            assert buf.first_flush_done is True

        _run(_test())

    def test_second_flush_no_reply(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext._prompt_message_ids["test-session-id"] = 42

        from extensions.telegram.extension import _StreamBuffer

        buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
        buf.first_flush_done = True  # Already flushed once
        buf.text_parts = ["More text"]
        ext._stream_buffers["test-session-id"] = buf

        async def _test():
            await ext._flush_stream_buffer("test-session-id")

            send_call = ext.app.bot.send_message.call_args
            assert "reply_parameters" not in send_call.kwargs

        _run(_test())


class TestReplyThreadingDisabled:
    """When reply_threading=false, no reply_parameters are sent."""

    def test_no_reply_when_disabled(self, ext):
        ext._reply_threading = False

        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext._prompt_message_ids["test-session-id"] = 42

        from extensions.telegram.extension import _StreamBuffer

        buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
        buf.text_parts = ["Hello"]
        ext._stream_buffers["test-session-id"] = buf

        async def _test():
            await ext._flush_stream_buffer("test-session-id")

            send_call = ext.app.bot.send_message.call_args
            assert "reply_parameters" not in send_call.kwargs

        _run(_test())


class TestReplyThreadingConfig:
    """Config option reply_threading controls threading."""

    def test_default_enabled(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {"token": "t", "allowed_users": [111], "working_dir": str(tmp_path)},
        )
        assert ext._reply_threading is True

    def test_explicit_disabled(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "token": "t",
                "allowed_users": [111],
                "working_dir": str(tmp_path),
                "reply_threading": False,
            },
        )
        assert ext._reply_threading is False


class TestFinalClearsPromptId:
    """is_final clears the stored prompt message_id."""

    def test_final_clears_prompt_id(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext._prompt_message_ids["test-session-id"] = 42

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.01, "num_turns": 1},
            )
            assert "test-session-id" not in ext._prompt_message_ids

        _run(_test())


class TestFallbackUsesReplyThreading:
    """When stream buffer was empty, fallback send uses reply threading."""

    def test_fallback_reply_threads(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext._prompt_message_ids["test-session-id"] = 42

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "Fallback text",
                {"is_final": True, "total_cost_usd": 0.01, "num_turns": 1},
            )
            # First send_message call should have reply_parameters (fallback text)
            first_call = ext.app.bot.send_message.call_args_list[0]
            assert first_call.kwargs.get("reply_parameters") is not None
            assert first_call.kwargs["reply_parameters"].message_id == 42

        _run(_test())


class TestSendChunkedReply:
    """_send_chunked passes reply_parameters only on first chunk."""

    def test_reply_on_first_chunk_only(self, ext):
        async def _test():
            # Send a text that requires 2 chunks
            long_text = "A" * 4001
            await ext._send_chunked(999, long_text, reply_to_message_id=42)

            calls = ext.app.bot.send_message.call_args_list
            assert len(calls) == 2
            # First chunk has reply_parameters
            assert calls[0].kwargs.get("reply_parameters") is not None
            # Second chunk does not
            assert "reply_parameters" not in calls[1].kwargs

        _run(_test())

    def test_no_reply_when_none(self, ext):
        async def _test():
            await ext._send_chunked(999, "Short text")

            send_call = ext.app.bot.send_message.call_args
            assert "reply_parameters" not in send_call.kwargs

        _run(_test())
