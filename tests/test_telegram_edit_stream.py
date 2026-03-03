"""Tests for Telegram edit-in-place streaming, tool grouping, and cost footer."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

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
    engine.session_manager.add_delivery_callback = MagicMock()
    engine.session_manager.list_mcp_tools = MagicMock(return_value={})
    engine.pending = MagicMock()
    engine.services = {}
    engine.events = MagicMock()
    engine.registry = None
    return engine


def _make_ext(engine, tmp_path, stream_mode="edit"):
    from extensions.telegram.extension import ExtensionImpl

    ext = ExtensionImpl()
    ext.configure(
        engine,
        {
            "token": "fake-token",
            "allowed_users": [111],
            "working_dir": str(tmp_path),
            "stream_mode": stream_mode,
        },
    )
    ext.app = MagicMock()
    ext.app.bot = MagicMock()
    ext.app.bot.send_message = AsyncMock()
    ext.app.bot.edit_message_text = AsyncMock()
    return ext


@pytest.fixture
def ext(engine, tmp_path):
    return _make_ext(engine, tmp_path, stream_mode="edit")


@pytest.fixture
def ext_multi(engine, tmp_path):
    return _make_ext(engine, tmp_path, stream_mode="multi")


def _make_session(
    slot=1,
    name="session-1",
    user_id="111",
    working_dir="/tmp/test",
    status=SessionStatus.IDLE,
):
    session = MagicMock()
    session.id = "test-session-id"
    session.slot = slot
    session.name = name
    session.user_id = user_id
    session.working_dir = working_dir
    session.status = status
    session.context = {"chat_id": 999}
    session.last_prompt = None
    return session


def _setup_session(ext):
    session = _make_session()
    ext.engine.session_manager.sessions = {"test-session-id": session}
    return session


# ── C1: _send_chunked returns message_id ────────────────────────────────


class TestSendChunkedReturnsMessageId:
    def test_returns_message_id(self, ext):
        msg = MagicMock()
        msg.message_id = 42
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            result = await ext._send_chunked(999, "Hello world")
            assert result == 42

        _run(_test())

    def test_returns_none_on_failure(self, ext):
        ext.app.bot.send_message = AsyncMock(side_effect=Exception("fail"))

        async def _test():
            result = await ext._send_chunked(999, "Hello world")
            assert result is None

        _run(_test())

    def test_returns_last_message_id_for_multipart(self, ext):
        """When text is split, return the last message_id."""
        msg1 = MagicMock()
        msg1.message_id = 10
        msg2 = MagicMock()
        msg2.message_id = 20
        ext.app.bot.send_message = AsyncMock(side_effect=[msg1, msg2])

        async def _test():
            long_text = "x" * 4500  # exceeds MAX_TG_MESSAGE
            result = await ext._send_chunked(999, long_text)
            assert result == 20

        _run(_test())


# ── C1: _send_html returns message_id ───────────────────────────────────


class TestSendHtmlReturnsMessageId:
    def test_returns_message_id(self, ext):
        msg = MagicMock()
        msg.message_id = 55
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            result = await ext._send_html(999, "Hello **bold** world")
            assert result == 55

        _run(_test())

    def test_returns_none_on_failure(self, ext):
        ext.app.bot.send_message = AsyncMock(side_effect=Exception("fail"))

        async def _test():
            result = await ext._send_html(999, "Hello world")
            assert result is None

        _run(_test())


# ── C1: Edit-in-place streaming ─────────────────────────────────────────


class TestStreamEditFlush:
    def test_first_flush_sends_new_message(self, ext):
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.text_parts = ["Hello ", "world"]
            ext._stream_buffers["test-session-id"] = buf

            result = await ext._stream_edit_flush("test-session-id")
            assert result is True
            assert buf.live_message_id == 100
            assert buf.live_text == "Hello world"
            assert buf.text_parts == []
            ext.app.bot.send_message.assert_called_once()

        _run(_test())

    def test_first_flush_sets_first_flush_done(self, ext):
        """First flush should set first_flush_done flag."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.text_parts = ["Hello"]
            ext._stream_buffers["test-session-id"] = buf
            assert buf.first_flush_done is False

            await ext._stream_edit_flush("test-session-id")
            assert buf.first_flush_done is True

        _run(_test())

    def test_subsequent_flush_edits_message(self, ext):
        _setup_session(ext)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Hello "
            buf.last_edit_time = 0  # long ago, no rate limit
            buf.text_parts = ["world"]
            ext._stream_buffers["test-session-id"] = buf

            result = await ext._stream_edit_flush("test-session-id")
            assert result is True
            assert buf.live_text == "Hello world"
            ext.app.bot.edit_message_text.assert_called_once()
            call_kwargs = ext.app.bot.edit_message_text.call_args[1]
            assert call_kwargs["message_id"] == 100
            assert call_kwargs["parse_mode"] == "HTML"

        _run(_test())

    def test_freeze_on_char_limit(self, ext):
        """When live_text approaches limit, freeze and start new message."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 200
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import EDIT_CHAR_LIMIT, _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            # Fill live_text close to limit; adding new text pushes over
            buf.live_text = "x" * (EDIT_CHAR_LIMIT - 10)
            buf.last_edit_time = 0
            buf.text_parts = ["y" * 100]  # pushes over EDIT_CHAR_LIMIT with prefix
            ext._stream_buffers["test-session-id"] = buf

            result = await ext._stream_edit_flush("test-session-id")
            assert result is True
            # Should have sent a new message (continuation)
            ext.app.bot.send_message.assert_called_once()
            assert buf.live_message_id == 200

        _run(_test())

    def test_rate_limit_defers_edit(self, ext):
        """Edits within MIN_EDIT_INTERVAL are deferred."""
        _setup_session(ext)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Hello "
            buf.last_edit_time = time.monotonic()  # just now
            buf.text_parts = ["world"]
            ext._stream_buffers["test-session-id"] = buf

            result = await ext._stream_edit_flush("test-session-id")
            assert result is True
            # Should NOT have called edit_message_text
            ext.app.bot.edit_message_text.assert_not_called()
            # Should have scheduled a retry task
            assert buf.flush_task is not None
            buf.flush_task.cancel()

        _run(_test())

    def test_message_not_modified_ignored(self, ext):
        """BadRequest 'Message is not modified' should be silently ignored."""
        _setup_session(ext)
        ext.app.bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Hello"
            buf.last_edit_time = 0
            buf.text_parts = [""]  # append nothing new — would cause "not modified"
            ext._stream_buffers["test-session-id"] = buf

            # Should not raise
            result = await ext._stream_edit_flush("test-session-id")
            assert result is True

        _run(_test())

    def test_message_deleted_falls_back(self, ext):
        """BadRequest 'message to edit not found' should send a new message."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 300
        ext.app.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("message to edit not found")
        )
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Hello "
            buf.last_edit_time = 0
            buf.text_parts = ["world"]
            ext._stream_buffers["test-session-id"] = buf

            result = await ext._stream_edit_flush("test-session-id")
            assert result is True
            # Fell back to new message
            assert buf.live_message_id == 300
            ext.app.bot.send_message.assert_called_once()

        _run(_test())


class TestDelayedFlushRouting:
    def test_edit_mode_routes_to_edit_flush(self, ext):
        """In edit mode, _delayed_flush should call _stream_edit_flush."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.text_parts = ["Hello"]
            ext._stream_buffers["test-session-id"] = buf

            # _delayed_flush will sleep then call edit flush
            # We patch sleep to skip waiting
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await ext._delayed_flush("test-session-id")

            # In edit mode, should have sent new message (first flush)
            ext.app.bot.send_message.assert_called_once()
            assert buf.live_message_id == 100

        _run(_test())

    def test_multi_mode_routes_to_regular_flush(self, ext_multi):
        """In multi mode, _delayed_flush should call _flush_stream_buffer."""
        _setup_session(ext_multi)
        msg = MagicMock()
        msg.message_id = 100
        ext_multi.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.text_parts = ["Hello"]
            ext_multi._stream_buffers["test-session-id"] = buf

            with patch("asyncio.sleep", new_callable=AsyncMock):
                await ext_multi._delayed_flush("test-session-id")

            # In multi mode, should have sent message and tracked message_id
            ext_multi.app.bot.send_message.assert_called_once()
            assert buf.last_sent_message_id == 100
            # Should NOT have set live_message_id
            assert buf.live_message_id is None

        _run(_test())


# ── C1: is_final with edit mode ─────────────────────────────────────────


class TestFinalEditMode:
    def test_final_appends_cost_footer_via_edit(self, ext):
        _setup_session(ext)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Some response text"
            buf.last_edit_time = 0
            ext._stream_buffers["test-session-id"] = buf

            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.1234, "num_turns": 4},
            )

            # Should have edited the live message with cost footer
            ext.app.bot.edit_message_text.assert_called()
            call_kwargs = ext.app.bot.edit_message_text.call_args[1]
            assert "$0.1234" in call_kwargs["text"]
            assert "4 turns" in call_kwargs["text"]
            assert call_kwargs["parse_mode"] == "HTML"

        _run(_test())


# ── C2: Tool call grouping ──────────────────────────────────────────────


class TestToolCallGrouping:
    def test_tool_events_buffered(self, ext):
        _setup_session(ext)

        async def _test():
            # First tool event
            await ext._deliver_result(
                "test-session-id",
                "",
                {
                    "is_stream": True,
                    "stream_type": "tool_use",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/foo/bar.py"},
                },
            )

            buf = ext._stream_buffers.get("test-session-id")
            assert buf is not None
            assert len(buf.tool_parts) == 1
            assert "Read" in buf.tool_parts[0]
            assert buf.tool_flush_task is not None

            # Second tool event
            await ext._deliver_result(
                "test-session-id",
                "",
                {
                    "is_stream": True,
                    "stream_type": "tool_use",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "def main"},
                },
            )

            assert len(buf.tool_parts) == 2
            # Cancel the flush task to prevent background execution
            buf.tool_flush_task.cancel()

        _run(_test())

    def test_tool_buffer_flushed_on_text_event(self, ext):
        """Tool buffer should be flushed when a text stream event arrives."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.tool_parts = [
                "\U0001f527 Read: /foo/bar.py",
                "\U0001f527 Grep: def main",
            ]
            ext._stream_buffers["test-session-id"] = buf

            # Send a text event — should flush tools first
            await ext._deliver_result(
                "test-session-id",
                "Some text",
                {"is_stream": True, "stream_type": "text"},
            )

            # Tools should have been flushed
            assert len(buf.tool_parts) == 0

        _run(_test())

    def test_tool_buffer_flushed_on_final(self, ext):
        """Tool buffer should be flushed on is_final."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.tool_parts = ["\U0001f527 Read: /foo/bar.py"]
            buf.live_message_id = 100
            buf.live_text = "response text"
            buf.last_edit_time = 0
            ext._stream_buffers["test-session-id"] = buf

            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.05, "num_turns": 2},
            )

            # Tool parts should be cleared
            # (buf is popped on is_final, so check the edit happened)
            assert "test-session-id" not in ext._stream_buffers

        _run(_test())

    def test_tool_buffer_flushed_on_stopped(self, ext):
        """Tool buffer should be flushed on is_stopped."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.tool_parts = ["\U0001f527 Read: /foo/bar.py"]
            ext._stream_buffers["test-session-id"] = buf

            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_stopped": True},
            )

            assert len(buf.tool_parts) == 0

        _run(_test())

    def test_tool_verbosity_none_skips(self, ext):
        """With verbosity=none, tool events should not be buffered."""
        _setup_session(ext)
        ext._user_stream_levels["111"] = "none"

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "",
                {
                    "is_stream": True,
                    "stream_type": "tool_use",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/foo.py"},
                },
            )

            # No buffer should have been created
            assert "test-session-id" not in ext._stream_buffers

        _run(_test())

    def test_tool_verbosity_mcp_filters(self, ext):
        """With verbosity=mcp, only MCP tools are buffered."""
        _setup_session(ext)
        ext._user_stream_levels["111"] = "mcp"

        async def _test():
            # Non-MCP tool — should be skipped
            await ext._deliver_result(
                "test-session-id",
                "",
                {
                    "is_stream": True,
                    "stream_type": "tool_use",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/foo.py"},
                },
            )
            assert "test-session-id" not in ext._stream_buffers

            # MCP tool — should be buffered
            await ext._deliver_result(
                "test-session-id",
                "",
                {
                    "is_stream": True,
                    "stream_type": "tool_use",
                    "tool_name": "mcp__vault__vault_list",
                    "tool_input": {},
                },
            )
            buf = ext._stream_buffers.get("test-session-id")
            assert buf is not None
            assert len(buf.tool_parts) == 1
            buf.tool_flush_task.cancel()

        _run(_test())


class TestToolFlushInEditMode:
    def test_tools_folded_into_live_message(self, ext):
        """In edit mode, tool summaries should be folded into the live message."""
        _setup_session(ext)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Some response"
            buf.last_edit_time = 0
            buf.tool_parts = ["\U0001f527 Read: /foo.py"]
            ext._stream_buffers["test-session-id"] = buf

            await ext._flush_tool_buffer("test-session-id")

            ext.app.bot.edit_message_text.assert_called_once()
            call_kwargs = ext.app.bot.edit_message_text.call_args[1]
            assert call_kwargs["message_id"] == 100
            assert call_kwargs["parse_mode"] == "HTML"

        _run(_test())

    def test_tools_sent_separately_when_no_live_message(self, ext):
        """Without live message, tools should be sent as separate message."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 200
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.tool_parts = ["\U0001f527 Read: /foo.py"]
            ext._stream_buffers["test-session-id"] = buf

            await ext._flush_tool_buffer("test-session-id")

            ext.app.bot.send_message.assert_called_once()
            assert buf.last_sent_message_id == 200

        _run(_test())


# ── C3: Cost footer integration ─────────────────────────────────────────


class TestCostFooterEditMode:
    def test_cost_footer_appended_to_live_message(self, ext):
        """In edit mode, cost footer is appended to live message."""
        _setup_session(ext)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Response text"
            buf.last_edit_time = 0
            ext._stream_buffers["test-session-id"] = buf

            prefix = ""  # single session, auto prefix = empty
            await ext._send_cost_footer("test-session-id", 999, prefix, 0.5678, 3)

            ext.app.bot.edit_message_text.assert_called_once()
            call_kwargs = ext.app.bot.edit_message_text.call_args[1]
            assert "$0.5678" in call_kwargs["text"]
            assert "3 turns" in call_kwargs["text"]
            assert call_kwargs["parse_mode"] == "HTML"
            assert buf.live_message_id is None  # cleaned up

        _run(_test())

    def test_cost_footer_fallback_on_edit_failure(self, ext):
        """If edit fails, cost footer is sent as separate message."""
        _setup_session(ext)
        ext.app.bot.edit_message_text = AsyncMock(side_effect=BadRequest("Something went wrong"))
        msg = MagicMock()
        msg.message_id = 200
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.live_message_id = 100
            buf.live_text = "Response text"
            buf.last_edit_time = 0
            ext._stream_buffers["test-session-id"] = buf

            prefix = ""
            await ext._send_cost_footer("test-session-id", 999, prefix, 0.1234, 2)

            # Should have fallen back to separate message
            ext.app.bot.send_message.assert_called_once()
            text = ext.app.bot.send_message.call_args[1]["text"]
            assert "$0.1234" in text

        _run(_test())


class TestCostFooterMultiMode:
    def test_cost_footer_edits_last_message(self, ext_multi):
        """In multi mode, cost footer edits the last sent message."""
        _setup_session(ext_multi)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.last_sent_message_id = 100
            buf.last_sent_text = "Hello world"
            ext_multi._stream_buffers["test-session-id"] = buf

            prefix = ""
            await ext_multi._send_cost_footer("test-session-id", 999, prefix, 0.2345, 5)

            ext_multi.app.bot.edit_message_text.assert_called_once()
            call_kwargs = ext_multi.app.bot.edit_message_text.call_args[1]
            assert "$0.2345" in call_kwargs["text"]
            assert "5 turns" in call_kwargs["text"]

        _run(_test())

    def test_cost_footer_falls_back_on_edit_error(self, ext_multi):
        """In multi mode, falls back to separate message if edit fails."""
        _setup_session(ext_multi)
        ext_multi.app.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("message to edit not found")
        )
        msg = MagicMock()
        msg.message_id = 200
        ext_multi.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.last_sent_message_id = 100
            buf.last_sent_text = "Hello"
            ext_multi._stream_buffers["test-session-id"] = buf

            prefix = ""
            await ext_multi._send_cost_footer("test-session-id", 999, prefix, 0.01, 1)

            ext_multi.app.bot.send_message.assert_called_once()
            text = ext_multi.app.bot.send_message.call_args[1]["text"]
            assert "$0.0100" in text

        _run(_test())

    def test_cost_footer_no_last_message_sends_separate(self, ext_multi):
        """With no tracked last message, sends cost as separate message."""
        _setup_session(ext_multi)
        msg = MagicMock()
        msg.message_id = 200
        ext_multi.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            ext_multi._stream_buffers["test-session-id"] = buf

            prefix = ""
            await ext_multi._send_cost_footer("test-session-id", 999, prefix, 0.05, 2)

            ext_multi.app.bot.send_message.assert_called_once()

        _run(_test())


# ── Cleanup in stop() and _cmd_delete ────────────────────────────────────


class TestCleanup:
    def test_stop_cancels_tool_flush_tasks(self, ext):
        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            # Create real tasks so we can check cancellation
            buf.flush_task = asyncio.create_task(asyncio.sleep(100))
            buf.tool_flush_task = asyncio.create_task(asyncio.sleep(100))
            ext._stream_buffers["test-session-id"] = buf

            await ext.stop()

            # Tasks are in "cancelling" state; allow one loop tick to propagate
            await asyncio.sleep(0)

            assert buf.flush_task.cancelled()
            assert buf.tool_flush_task.cancelled()
            assert len(ext._stream_buffers) == 0

        _run(_test())


# ── Stream mode config ──────────────────────────────────────────────────


class TestStreamModeConfig:
    def test_default_stream_mode_is_edit(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "token": "fake-token",
                "allowed_users": [111],
                "working_dir": str(tmp_path),
            },
        )
        assert ext._stream_mode == "edit"

    def test_explicit_multi_mode(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "token": "fake-token",
                "allowed_users": [111],
                "working_dir": str(tmp_path),
                "stream_mode": "multi",
            },
        )
        assert ext._stream_mode == "multi"


# ── Integration: full stream → final flow ────────────────────────────────


class TestFullEditStreamFlow:
    def test_text_stream_then_final_with_cost(self, ext):
        """Full flow: text events → final with cost footer edited in-place."""
        _setup_session(ext)
        msg = MagicMock()
        msg.message_id = 100
        ext.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            # Stream text event
            await ext._deliver_result(
                "test-session-id",
                "Hello ",
                {"is_stream": True, "stream_type": "text"},
            )
            buf = ext._stream_buffers["test-session-id"]
            assert buf.text_parts == ["Hello "]

            # Simulate flush (would normally be delayed)
            await ext._stream_edit_flush("test-session-id")
            assert buf.live_message_id == 100
            assert buf.live_text == "Hello "

            # Another text event
            await ext._deliver_result(
                "test-session-id",
                "world",
                {"is_stream": True, "stream_type": "text"},
            )
            # Manually flush again
            buf.last_edit_time = 0  # reset rate limit
            await ext._stream_edit_flush("test-session-id")
            assert buf.live_text == "Hello world"
            ext.app.bot.edit_message_text.assert_called()

            # Final event with cost
            buf.last_edit_time = 0  # reset rate limit
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.0500, "num_turns": 3},
            )

            # Cost footer should have been edited into the live message
            last_edit = ext.app.bot.edit_message_text.call_args[1]
            assert "$0.0500" in last_edit["text"]
            assert "3 turns" in last_edit["text"]
            # Buffer should be cleaned up
            assert "test-session-id" not in ext._stream_buffers

        _run(_test())


class TestFullMultiStreamFlow:
    def test_text_stream_then_final_edits_last_message(self, ext_multi):
        """Full flow in multi mode: cost footer edits the last sent message."""
        _setup_session(ext_multi)
        msg = MagicMock()
        msg.message_id = 100
        ext_multi.app.bot.send_message = AsyncMock(return_value=msg)

        async def _test():
            from extensions.telegram.extension import _StreamBuffer

            buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
            buf.text_parts = ["Hello world"]
            ext_multi._stream_buffers["test-session-id"] = buf

            # Flush in multi mode
            await ext_multi._flush_stream_buffer("test-session-id")
            assert buf.last_sent_message_id == 100

            # Final event with cost
            await ext_multi._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.0100, "num_turns": 1},
            )

            # Should have edited the last message
            ext_multi.app.bot.edit_message_text.assert_called_once()
            call_kwargs = ext_multi.app.bot.edit_message_text.call_args[1]
            assert "$0.0100" in call_kwargs["text"]

        _run(_test())
