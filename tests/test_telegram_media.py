"""Tests for Telegram media (photo/document) handling."""

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
        engine, {"token": "fake-token", "allowed_users": [111], "working_dir": str(tmp_path)}
    )
    # Don't start the full app — just wire up enough for unit tests
    ext.app = MagicMock()
    ext.app.bot = MagicMock()
    ext.app.bot.get_file = AsyncMock()
    ext.app.bot.send_message = AsyncMock()
    return ext


def _make_session(
    slot=1, name="session-1", user_id="111", working_dir="/tmp/test", status=SessionStatus.IDLE
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


def _make_update(user_id=111, chat_id=999):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.caption = None
    return update


class TestPhotoDownloadAndPrompt:
    def test_photo_download_and_prompt(self, ext, tmp_path):
        session = _make_session(working_dir=str(tmp_path))
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update()
        # Set up photo
        photo_size = MagicMock()
        photo_size.file_id = "photo-file-id"
        photo_size.file_unique_id = "uniq123"
        update.message.photo = [photo_size]
        update.message.document = None

        # Mock file download
        tg_file = MagicMock()
        tg_file.download_to_drive = AsyncMock()
        ext.app.bot.get_file = AsyncMock(return_value=tg_file)

        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_media(update, ctx)

            # Verify download
            ext.app.bot.get_file.assert_called_once_with("photo-file-id")
            dest = tg_file.download_to_drive.call_args[0][0]
            assert "photo_uniq123.jpg" in dest
            assert ".claude-ext-uploads" in dest

            # Verify prompt sent
            ext.engine.session_manager.send_prompt.assert_called_once()
            prompt = ext.engine.session_manager.send_prompt.call_args[0][1]
            assert "[The user sent an image:" in prompt
            assert "Please analyze this image." in prompt

        _run(_test())


class TestDocumentDownloadAndPrompt:
    def test_document_download_and_prompt(self, ext, tmp_path):
        session = _make_session(working_dir=str(tmp_path))
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update()
        update.message.photo = None
        update.message.document = MagicMock()
        update.message.document.file_id = "doc-file-id"
        update.message.document.file_unique_id = "docuniq456"
        update.message.document.file_name = "report.pdf"

        tg_file = MagicMock()
        tg_file.download_to_drive = AsyncMock()
        ext.app.bot.get_file = AsyncMock(return_value=tg_file)

        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_media(update, ctx)

            ext.app.bot.get_file.assert_called_once_with("doc-file-id")
            dest = tg_file.download_to_drive.call_args[0][0]
            assert "report_docuniq456.pdf" in dest

            prompt = ext.engine.session_manager.send_prompt.call_args[0][1]
            assert "[The user sent a file:" in prompt
            assert "Please analyze this file." in prompt

        _run(_test())


class TestPhotoWithCaption:
    def test_photo_with_caption(self, ext, tmp_path):
        session = _make_session(working_dir=str(tmp_path))
        ext.engine.session_manager.sessions = {"test-session-id": session}

        update = _make_update()
        photo_size = MagicMock()
        photo_size.file_id = "photo-id"
        photo_size.file_unique_id = "uniq789"
        update.message.photo = [photo_size]
        update.message.document = None
        update.message.caption = "What's in this screenshot?"

        tg_file = MagicMock()
        tg_file.download_to_drive = AsyncMock()
        ext.app.bot.get_file = AsyncMock(return_value=tg_file)

        ctx = MagicMock()
        ctx.user_data = {"active_session_id": "test-session-id"}

        async def _test():
            await ext._handle_media(update, ctx)

            prompt = ext.engine.session_manager.send_prompt.call_args[0][1]
            assert "What's in this screenshot?" in prompt
            assert "Please analyze this image." not in prompt

        _run(_test())


class TestUnsupportedMessageType:
    def test_unsupported_message_type(self, ext):
        update = _make_update()
        ctx = MagicMock()

        async def _test():
            await ext._handle_unsupported(update, ctx)
            update.message.reply_text.assert_called_once_with(
                "Unsupported message type. Send text, images, or files."
            )

        _run(_test())


class TestMediaNoSession:
    def test_media_auto_creates_session(self, ext, tmp_path):
        """When no session exists, _ensure_active_session creates one."""
        new_session = _make_session(working_dir=str(tmp_path))
        ext.engine.session_manager.create_session = AsyncMock(return_value=new_session)
        ext.engine.session_manager.sessions = {"test-session-id": new_session}

        update = _make_update()
        photo_size = MagicMock()
        photo_size.file_id = "photo-id"
        photo_size.file_unique_id = "uniqabc"
        update.message.photo = [photo_size]
        update.message.document = None

        tg_file = MagicMock()
        tg_file.download_to_drive = AsyncMock()
        ext.app.bot.get_file = AsyncMock(return_value=tg_file)

        ctx = MagicMock()
        ctx.user_data = {}

        async def _test():
            await ext._handle_media(update, ctx)
            # Session was created
            ext.engine.session_manager.create_session.assert_called_once()
            # Prompt was sent
            ext.engine.session_manager.send_prompt.assert_called_once()
            # Reply mentions auto-activation
            reply_text = update.message.reply_text.call_args[0][0]
            assert "auto-activated" in reply_text

        _run(_test())
