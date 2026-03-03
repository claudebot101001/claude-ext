"""Tests for Telegram smart session prefix (Phase B2)."""

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


class TestSessionPrefixAuto:
    """Auto mode: prefix only when user has 2+ sessions."""

    def test_single_session_no_prefix(self, ext):
        session = _make_session()
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]
        assert ext._session_prefix(session, "111") == ""

    def test_multiple_sessions_has_prefix(self, ext):
        s1 = _make_session(slot=1, name="session-1")
        s2 = _make_session(slot=2, name="session-2")
        ext.engine.session_manager.get_sessions_for_user.return_value = [s1, s2]
        assert ext._session_prefix(s1, "111") == "[#1 session-1] "

    def test_default_config_is_auto(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {"token": "t", "allowed_users": [111], "working_dir": str(tmp_path)},
        )
        assert ext._show_prefix == "auto"


class TestSessionPrefixAlways:
    """Always mode: prefix shown regardless of session count."""

    def test_always_shows_prefix(self, ext):
        ext._show_prefix = "always"
        session = _make_session()
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]
        assert ext._session_prefix(session, "111") == "[#1 session-1] "


class TestSessionPrefixNever:
    """Never mode: prefix hidden regardless of session count."""

    def test_never_shows_prefix(self, ext):
        ext._show_prefix = "never"
        s1 = _make_session(slot=1, name="session-1")
        s2 = _make_session(slot=2, name="session-2")
        ext.engine.session_manager.get_sessions_for_user.return_value = [s1, s2]
        assert ext._session_prefix(s1, "111") == ""


class TestPrefixConfig:
    """Config option show_prefix is read correctly."""

    def test_config_always(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "token": "t",
                "allowed_users": [111],
                "working_dir": str(tmp_path),
                "show_prefix": "always",
            },
        )
        assert ext._show_prefix == "always"

    def test_config_never(self, engine, tmp_path):
        from extensions.telegram.extension import ExtensionImpl

        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "token": "t",
                "allowed_users": [111],
                "working_dir": str(tmp_path),
                "show_prefix": "never",
            },
        )
        assert ext._show_prefix == "never"


class TestPrefixInDelivery:
    """Prefix is used correctly in delivery paths."""

    def test_heartbeat_uses_prefix(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        # Single session — auto mode means no prefix
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_heartbeat": True, "elapsed_s": 120},
            )
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent == "<i>Still working... (2m elapsed)</i>"

        _run(_test())

    def test_heartbeat_with_prefix(self, ext):
        s1 = _make_session(slot=1, name="session-1")
        s2 = _make_session(slot=2, name="session-2")
        ext.engine.session_manager.sessions = {"test-session-id": s1}
        ext.engine.session_manager.get_sessions_for_user.return_value = [s1, s2]

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_heartbeat": True, "elapsed_s": 120},
            )
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent == "[#1 session-1] <i>Still working... (2m elapsed)</i>"

        _run(_test())

    def test_stopped_no_prefix_single_session(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_stopped": True},
            )
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent == "<b>Stopped</b>"

        _run(_test())

    def test_error_with_prefix(self, ext):
        s1 = _make_session(slot=1, name="session-1")
        s2 = _make_session(slot=2, name="session-2")
        ext.engine.session_manager.sessions = {"test-session-id": s1}
        ext.engine.session_manager.get_sessions_for_user.return_value = [s1, s2]

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "Something broke",
                {"is_final": True, "is_error": True},
            )
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent.startswith("[#1 session-1] <b>")
            assert "Something broke" in sent

        _run(_test())

    def test_cost_footer_no_prefix_single(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "",
                {"is_final": True, "total_cost_usd": 0.1234, "num_turns": 4},
            )
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent == "<i>--- $0.1234 | 4 turns ---</i>"

        _run(_test())


class TestPrefixInStreamFlush:
    """Prefix in stream buffer flush respects auto mode."""

    def test_stream_flush_no_prefix_single(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]

        from extensions.telegram.extension import _StreamBuffer

        buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
        buf.text_parts = ["Hello"]
        ext._stream_buffers["test-session-id"] = buf

        async def _test():
            await ext._flush_stream_buffer("test-session-id")
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent == "Hello"

        _run(_test())

    def test_stream_flush_with_prefix_multi(self, ext):
        s1 = _make_session(slot=1, name="session-1")
        s2 = _make_session(slot=2, name="session-2")
        ext.engine.session_manager.sessions = {"test-session-id": s1}
        ext.engine.session_manager.get_sessions_for_user.return_value = [s1, s2]

        from extensions.telegram.extension import _StreamBuffer

        buf = _StreamBuffer(chat_id=999, slot=1, name="session-1", user_id="111")
        buf.text_parts = ["Hello"]
        ext._stream_buffers["test-session-id"] = buf

        async def _test():
            await ext._flush_stream_buffer("test-session-id")
            sent = ext.app.bot.send_message.call_args.kwargs["text"]
            assert sent == "[#1 session-1] Hello"

        _run(_test())


class TestPrefixInHandleMessage:
    """_handle_message uses smart prefix in the Processing reply."""

    def test_processing_no_prefix_single(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]

        update = MagicMock()
        update.effective_user.id = 111
        update.effective_chat.id = 999
        update.message.reply_text = AsyncMock()
        update.message.text = "Hello"
        update.message.message_id = 1

        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_message(update, ctx)
            reply = update.message.reply_text.call_args[0][0]
            # Single session — tag falls back to bracketed form (no prefix from _session_prefix)
            assert "Processing..." in reply

        _run(_test())

    def test_processing_with_prefix_multi(self, ext):
        s1 = _make_session(slot=1, name="session-1")
        s2 = _make_session(slot=2, name="session-2")
        ext.engine.session_manager.sessions = {"test-session-id": s1}
        ext.engine.session_manager.get_sessions_for_user.return_value = [s1, s2]

        update = MagicMock()
        update.effective_user.id = 111
        update.effective_chat.id = 999
        update.message.reply_text = AsyncMock()
        update.message.text = "Hello"
        update.message.message_id = 1

        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_message(update, ctx)
            reply = update.message.reply_text.call_args[0][0]
            assert reply.startswith("[#1 session-1]")
            assert "Processing..." in reply

        _run(_test())


class TestPrefixInQuestion:
    """ask_user question uses smart prefix."""

    def test_question_no_prefix_single(self, ext):
        session = _make_session()
        ext.engine.session_manager.sessions = {"test-session-id": session}
        ext.engine.session_manager.get_sessions_for_user.return_value = [session]

        async def _test():
            await ext._deliver_result(
                "test-session-id",
                "Pick one?",
                {"is_question": True, "request_id": "req1"},
            )
            sent = ext.app.bot.send_message.call_args
            text = sent.args[1] if len(sent.args) > 1 else sent.kwargs.get("text", sent.args[0])
            # With no prefix, text should just be the question
            assert "\u2753 Pick one?" in str(text)
            assert "[#1 session-1]" not in str(text)

        _run(_test())
