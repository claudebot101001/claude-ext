"""Tests for browser extension (thin CLI extension pattern)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from extensions.browser.extension import ExtensionImpl, _SYSTEM_PROMPT


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture
def engine():
    engine = MagicMock()
    engine.session_manager = MagicMock()
    return engine


@pytest.fixture
def ext(engine):
    e = ExtensionImpl()
    e.configure(engine, {})
    return e


class TestConfigure:
    def test_default_binary(self, engine):
        e = ExtensionImpl()
        e.configure(engine, {})
        assert e._binary == "agent-browser"

    def test_custom_binary(self, engine):
        e = ExtensionImpl()
        e.configure(engine, {"binary": "/usr/local/bin/agent-browser"})
        assert e._binary == "/usr/local/bin/agent-browser"

    def test_reconfigure(self, ext):
        ext.reconfigure({"binary": "/opt/bin/ab"})
        assert ext._binary == "/opt/bin/ab"


class TestStart:
    def test_registers_system_prompt(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_system_prompt.assert_called_once_with(
            _SYSTEM_PROMPT, mcp_server="browser"
        )

    @patch("shutil.which", return_value=None)
    def test_warns_when_binary_missing(self, mock_which, ext):
        """Extension starts successfully even if binary is not found."""
        _run(ext.start())
        # System prompt is still registered (graceful degradation)
        ext.engine.session_manager.add_system_prompt.assert_called_once()

    @patch("shutil.which", return_value="/usr/bin/agent-browser")
    def test_starts_when_binary_found(self, mock_which, ext):
        _run(ext.start())
        ext.engine.session_manager.add_system_prompt.assert_called_once()


class TestHealthCheck:
    @patch("shutil.which", return_value=None)
    def test_degraded_when_binary_missing(self, mock_which, ext):
        result = _run(ext.health_check())
        assert result["status"] == "degraded"
        assert "not found" in result["detail"]

    @patch("shutil.which", return_value="/usr/bin/agent-browser")
    def test_ok_when_binary_found(self, mock_which, ext):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = MagicMock(return_value=(b"", b""))

            async def fake_communicate():
                return b"", b""

            proc.communicate = fake_communicate
            mock_exec.return_value = proc
            result = _run(ext.health_check())
        assert result["status"] == "ok"
        assert result["daemon_running"] is True

    @patch("shutil.which", return_value="/usr/bin/agent-browser")
    def test_ok_daemon_not_running(self, mock_which, ext):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = MagicMock()
            proc.returncode = 1

            async def fake_communicate():
                return b"", b"error"

            proc.communicate = fake_communicate
            mock_exec.return_value = proc
            result = _run(ext.health_check())
        assert result["status"] == "ok"
        assert result["daemon_running"] is False


class TestSystemPrompt:
    def test_prompt_contains_core_workflow(self):
        assert "agent-browser open" in _SYSTEM_PROMPT
        assert "snapshot -i" in _SYSTEM_PROMPT
        assert "@e1" in _SYSTEM_PROMPT
        assert "--help" in _SYSTEM_PROMPT

    def test_prompt_mentions_ref_invalidation(self):
        assert "invalidate" in _SYSTEM_PROMPT.lower()
