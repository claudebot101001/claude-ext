"""Tests for browser extension (CLI + scraping MCP)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from extensions.browser.extension import _SYSTEM_PROMPT, ExtensionImpl
from extensions.browser.mcp_server import BrowserMCPServer


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


# -- Extension tests -----------------------------------------------------------


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

    def test_registers_mcp_server(self, ext):
        _run(ext.start())
        calls = ext.engine.session_manager.register_mcp_server.call_args_list
        # Should register both browser (scraping) and stealth_browser
        server_names = [c[0][0] for c in calls]
        assert "browser" in server_names
        browser_call = next(c for c in calls if c[0][0] == "browser")
        tools = browser_call[1].get("tools") or browser_call[0][2]
        assert len(tools) == 3
        tool_names = {t["name"] for t in tools}
        assert tool_names == {"scrape", "scrape_stealth", "scrape_extract"}

    @patch("shutil.which", return_value=None)
    def test_warns_when_binary_missing(self, mock_which, ext):
        """Extension starts successfully even if binary is not found."""
        _run(ext.start())
        # System prompt and MCP server are still registered (graceful degradation)
        ext.engine.session_manager.add_system_prompt.assert_called_once()
        assert ext.engine.session_manager.register_mcp_server.call_count >= 1

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


# -- MCP Server tests ----------------------------------------------------------


class TestMCPServerSchema:
    def test_server_name(self):
        server = BrowserMCPServer()
        assert server.name == "browser"

    def test_gateway_description(self):
        server = BrowserMCPServer()
        assert server.gateway_description
        assert (
            "scraping" in server.gateway_description.lower()
            or "fetch" in server.gateway_description.lower()
        )

    def test_has_three_tools(self):
        server = BrowserMCPServer()
        assert len(server.tools) == 3

    def test_tool_names(self):
        server = BrowserMCPServer()
        names = {t["name"] for t in server.tools}
        assert names == {"scrape", "scrape_stealth", "scrape_extract"}

    def test_all_tools_require_url(self):
        server = BrowserMCPServer()
        for tool in server.tools:
            required = tool["inputSchema"].get("required", [])
            assert "url" in required or "selectors" in required

    def test_handlers_match_tools(self):
        server = BrowserMCPServer()
        tool_names = {t["name"] for t in server.tools}
        handler_names = set(server.handlers.keys())
        assert tool_names == handler_names


class TestMCPServerHandlers:
    def test_scrape_missing_url(self):
        server = BrowserMCPServer()
        result = server._handle_scrape({})
        assert "Error" in result

    def test_scrape_stealth_missing_url(self):
        server = BrowserMCPServer()
        result = server._handle_scrape_stealth({})
        assert "Error" in result

    def test_scrape_extract_missing_url(self):
        server = BrowserMCPServer()
        result = server._handle_scrape_extract({})
        assert "Error" in result

    def test_scrape_extract_missing_selectors(self):
        server = BrowserMCPServer()
        result = server._handle_scrape_extract({"url": "http://example.com"})
        assert "Error" in result
