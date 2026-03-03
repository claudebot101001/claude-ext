"""Tests for Telegram send_message_draft streaming feature."""

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
        {
            "token": "fake-token",
            "allowed_users": [111],
            "working_dir": str(tmp_path),
            "streaming": "partial",
        },
    )
    # Don't start the full app — just wire up enough for unit tests
    ext.app = MagicMock()
    ext.app.bot = MagicMock()
    ext.app.bot.send_message = AsyncMock()
    ext.app.bot.send_message_draft = AsyncMock(return_value=True)
    return ext


@pytest.fixture
def ext_no_streaming(engine, tmp_path):
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
    ext.app = MagicMock()
    ext.app.bot = MagicMock()
    ext.app.bot.send_message = AsyncMock()
    ext.app.bot.send_message_draft = AsyncMock(return_value=True)
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


class TestDraftDisabledByDefault:
    def test_draft_disabled_by_default(self, ext_no_streaming):
        """Without streaming config, send_message_draft is never called."""
        session = _make_session()
        ext_no_streaming.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            # Send text stream events
            await ext_no_streaming._deliver_result(
                "test-session-id",
                "Hello world",
                {"is_stream": True, "stream_type": "text"},
            )
            # Advance past standard flush delay
            await asyncio.sleep(2.1)

            ext_no_streaming.app.bot.send_message_draft.assert_not_called()
            ext_no_streaming.app.bot.send_message.assert_called()

        _run(_run_test())


class TestDraftSendsOnTextStream:
    def test_draft_sends_on_text_stream(self, ext):
        """Draft is sent after 0.5s when text events arrive."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            await ext._deliver_result(
                "test-session-id",
                "Hello ",
                {"is_stream": True, "stream_type": "text"},
            )
            await ext._deliver_result(
                "test-session-id",
                "world",
                {"is_stream": True, "stream_type": "text"},
            )
            # Advance past draft flush delay
            await asyncio.sleep(0.6)

            ext.app.bot.send_message_draft.assert_called()
            call_kwargs = ext.app.bot.send_message_draft.call_args[1]
            assert call_kwargs["chat_id"] == 999
            assert call_kwargs["draft_id"] != 0
            assert "Hello " in call_kwargs["text"]
            assert "world" in call_kwargs["text"]

        _run(_run_test())


class TestDraftFinalSendsMessage:
    def test_draft_final_sends_message(self, ext):
        """is_final event triggers send_message (not just draft), buffer cleaned up."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            await ext._deliver_result(
                "test-session-id",
                "Some response text",
                {"is_stream": True, "stream_type": "text"},
            )
            # Deliver final event (should flush buffer via send_message)
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.001, "num_turns": 1},
            )

            ext.app.bot.send_message.assert_called()
            # Buffer should be cleaned up
            assert "test-session-id" not in ext._stream_buffers

        _run(_run_test())


class TestDraftFallbackOnFailure:
    def test_draft_fallback_on_failure(self, ext):
        """When send_message_draft raises, falls back to standard send_message."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.app.bot.send_message_draft = AsyncMock(side_effect=Exception("API error"))

        async def _run_test():
            await ext._deliver_result(
                "test-session-id",
                "Some text",
                {"is_stream": True, "stream_type": "text"},
            )
            # Advance past draft delay (draft fails, triggers standard flush scheduling)
            await asyncio.sleep(0.6)

            buf = ext._stream_buffers.get("test-session-id")
            assert buf is not None
            assert buf.draft_failed is True

            # Advance past standard flush delay
            await asyncio.sleep(2.1)

            ext.app.bot.send_message.assert_called()

        _run(_run_test())


class TestDraftIdUnique:
    def test_draft_id_unique(self, ext):
        """Different sessions get different draft IDs."""
        session_a = _make_session(session_id="session-a", slot=1, name="a")
        session_b = _make_session(session_id="session-b", slot=2, name="b")
        ext.engine.session_manager.sessions = {
            "session-a": session_a,
            "session-b": session_b,
        }

        async def _run_test():
            await ext._deliver_result(
                "session-a",
                "Text A",
                {"is_stream": True, "stream_type": "text"},
            )
            await ext._deliver_result(
                "session-b",
                "Text B",
                {"is_stream": True, "stream_type": "text"},
            )

            buf_a = ext._stream_buffers.get("session-a")
            buf_b = ext._stream_buffers.get("session-b")
            assert buf_a is not None
            assert buf_b is not None
            assert buf_a.draft_id != 0
            assert buf_b.draft_id != 0
            assert buf_a.draft_id != buf_b.draft_id

        _run(_run_test())


class TestDraftResetOnToolUse:
    def test_draft_reset_on_tool_use(self, ext):
        """tool_use event flushes text, next text segment gets a new draft_id."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            # First text segment
            await ext._deliver_result(
                "test-session-id",
                "First part",
                {"is_stream": True, "stream_type": "text"},
            )
            first_draft_id = ext._stream_buffers["test-session-id"].draft_id

            # Tool use event — flushes text, rotates draft_id
            await ext._deliver_result(
                "test-session-id",
                "bash(echo hello)",
                {
                    "is_stream": True,
                    "stream_type": "tool_use",
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                },
            )
            # send_message should have been called for the flush
            ext.app.bot.send_message.assert_called()

            # Now send more text — should get a new draft_id
            await ext._deliver_result(
                "test-session-id",
                "Second part",
                {"is_stream": True, "stream_type": "text"},
            )
            second_draft_id = ext._stream_buffers["test-session-id"].draft_id
            assert second_draft_id != first_draft_id
            assert second_draft_id > first_draft_id

        _run(_run_test())


class TestDraftCleanupOnStopMethod:
    def test_draft_cleanup_on_stop_method(self, ext):
        """stop() cancels all pending draft_tasks."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            # Create a buffer with a draft_task
            await ext._deliver_result(
                "test-session-id",
                "Some text",
                {"is_stream": True, "stream_type": "text"},
            )
            buf = ext._stream_buffers.get("test-session-id")
            assert buf is not None
            assert buf.draft_task is not None
            draft_task = buf.draft_task

            # Stop the extension (mocking the app stop)
            ext.app.updater = MagicMock()
            ext.app.updater.stop = AsyncMock()
            ext.app.stop = AsyncMock()
            ext.app.shutdown = AsyncMock()

            await ext.stop()
            # Yield to event loop so cancellation is processed
            await asyncio.sleep(0)

            assert draft_task.cancelled()

        _run(_run_test())


class TestDraftCleanupOnDelete:
    def test_draft_cleanup_on_delete(self, ext):
        """_cmd_delete cancels draft_task for the deleted session."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.get_session_by_slot = MagicMock(return_value=session)
        ext.engine.session_manager.get_sessions_for_user = MagicMock(return_value=[session])
        ext.engine.session_manager.destroy_session = AsyncMock()

        async def _run_test():
            # Create a buffer with a draft_task
            await ext._deliver_result(
                "test-session-id",
                "Some text",
                {"is_stream": True, "stream_type": "text"},
            )
            buf = ext._stream_buffers.get("test-session-id")
            assert buf is not None
            assert buf.draft_task is not None
            draft_task = buf.draft_task

            # Simulate delete command
            update = MagicMock()
            update.effective_user.id = 111
            update.message.text = "/delete 1"
            update.message.reply_text = AsyncMock()
            ctx = MagicMock()
            ctx.user_data = {}

            await ext._cmd_delete(update, ctx)
            # Yield to event loop so cancellation is processed
            await asyncio.sleep(0)

            assert draft_task.cancelled()
            assert "test-session-id" not in ext._stream_buffers

        _run(_run_test())


class TestDraftTextTruncation:
    def test_draft_text_truncation(self, ext):
        """Draft text longer than MAX_TG_MESSAGE is truncated."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            # Send a very long text
            long_text = "A" * 5000
            await ext._deliver_result(
                "test-session-id",
                long_text,
                {"is_stream": True, "stream_type": "text"},
            )
            await asyncio.sleep(0.6)

            ext.app.bot.send_message_draft.assert_called()
            call_kwargs = ext.app.bot.send_message_draft.call_args[1]
            # MAX_TG_MESSAGE = 4000, truncated to that + "..."
            assert len(call_kwargs["text"]) <= 4003  # MAX_TG_MESSAGE + len("...")

        _run(_run_test())


class TestDraftNoSendForEmpty:
    def test_draft_no_send_for_empty(self, ext):
        """Whitespace-only text events do not trigger send_message_draft."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            await ext._deliver_result(
                "test-session-id",
                "   \n\t  ",
                {"is_stream": True, "stream_type": "text"},
            )
            await asyncio.sleep(0.6)

            ext.app.bot.send_message_draft.assert_not_called()

        _run(_run_test())


class TestDraftStoppedFlushesMessage:
    def test_draft_stopped_flushes_message(self, ext):
        """is_stopped event sends accumulated text via send_message."""
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}

        async def _run_test():
            await ext._deliver_result(
                "test-session-id",
                "Partial response",
                {"is_stream": True, "stream_type": "text"},
            )
            # Deliver stopped event
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_stopped": True},
            )

            # send_message should be called (flush + "Task stopped.")
            ext.app.bot.send_message.assert_called()
            # Check that at least one call contains the accumulated text
            all_texts = [call[1]["text"] for call in ext.app.bot.send_message.call_args_list]
            assert any("Partial response" in t for t in all_texts)

        _run(_run_test())
