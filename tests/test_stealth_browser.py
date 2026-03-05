"""Tests for stealth browser MCP server + extension integration."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from extensions.browser.extension import ExtensionImpl


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def ext_stealth_disabled(engine):
    e = ExtensionImpl()
    e.configure(engine, {"stealth": {"enabled": False}})
    return e


@pytest.fixture
def ext_stealth_nopecha(engine):
    e = ExtensionImpl()
    e.configure(engine, {"stealth": {"enabled": True, "captcha_solver": "nopecha"}})
    return e


# ---------------------------------------------------------------------------
# Extension registration tests
# ---------------------------------------------------------------------------


class TestStealthRegistration:
    def test_registers_stealth_server_by_default(self, ext):
        _run(ext.start())
        calls = ext.engine.session_manager.register_mcp_server.call_args_list
        server_names = [c[0][0] for c in calls]
        assert "stealth_browser" in server_names

    def test_stealth_disabled_skips_registration(self, ext_stealth_disabled):
        _run(ext_stealth_disabled.start())
        calls = ext_stealth_disabled.engine.session_manager.register_mcp_server.call_args_list
        server_names = [c[0][0] for c in calls]
        assert "stealth_browser" not in server_names
        assert "browser" in server_names  # scraping still registered

    def test_stealth_server_has_25_tools(self, ext):
        _run(ext.start())
        calls = ext.engine.session_manager.register_mcp_server.call_args_list
        stealth_call = next(c for c in calls if c[0][0] == "stealth_browser")
        tools = stealth_call[1].get("tools") or stealth_call[0][2]
        assert len(tools) == 25

    def test_stealth_tool_names(self, ext):
        _run(ext.start())
        calls = ext.engine.session_manager.register_mcp_server.call_args_list
        stealth_call = next(c for c in calls if c[0][0] == "stealth_browser")
        tools = stealth_call[1].get("tools") or stealth_call[0][2]
        names = {t["name"] for t in tools}
        expected = {
            "open",
            "goto",
            "snapshot",
            "click",
            "fill",
            "select",
            "type",
            "press",
            "wait",
            "evaluate",
            "screenshot",
            "get_url",
            "get_title",
            "get_text",
            "upload",
            "download",
            "switch_tab",
            "switch_frame",
            "add_auth_domain",
            "create_profile",
            "list_profiles",
            "delete_profile",
            "close",
            "check_email",
            "read_email",
        }
        assert names == expected

    def test_stealth_config_passed_as_env(self, ext_stealth_nopecha):
        _run(ext_stealth_nopecha.start())
        calls = ext_stealth_nopecha.engine.session_manager.register_mcp_server.call_args_list
        stealth_call = next(c for c in calls if c[0][0] == "stealth_browser")
        server_config = stealth_call[0][1]
        env = server_config["env"]
        config = json.loads(env["STEALTH_BROWSER_CONFIG"])
        assert config["captcha_solver"] == "nopecha"

    def test_reconfigure_updates_stealth_config(self, ext):
        ext.reconfigure({"stealth": {"idle_timeout": 600}})
        assert ext._stealth_config["idle_timeout"] == 600


class TestStealthHealthCheck:
    @patch("shutil.which", return_value="/usr/bin/agent-browser")
    def test_health_includes_stealth_fields(self, mock_which, ext):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = MagicMock()
            proc.returncode = 0

            async def fake_communicate():
                return b"", b""

            proc.communicate = fake_communicate
            mock_exec.return_value = proc
            result = _run(ext.health_check())
        assert "stealth_enabled" in result
        assert result["stealth_enabled"] is True

    @patch("shutil.which", return_value="/usr/bin/agent-browser")
    def test_health_stealth_disabled(self, mock_which, ext_stealth_disabled):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = MagicMock()
            proc.returncode = 0

            async def fake_communicate():
                return b"", b""

            proc.communicate = fake_communicate
            mock_exec.return_value = proc
            result = _run(ext_stealth_disabled.health_check())
        assert result["stealth_enabled"] is False


# ---------------------------------------------------------------------------
# StealthBrowserMCPServer unit tests (no real browser)
# ---------------------------------------------------------------------------


class TestStealthMCPServerSchema:
    def test_import_server(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        assert server.name == "stealth_browser"
        # Cleanup the background thread
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)

    def test_gateway_description(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        assert "stealth" in server.gateway_description.lower()
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)

    def test_has_25_tools(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        assert len(server.tools) == 25
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)

    def test_handlers_match_tools(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        tool_names = {t["name"] for t in server.tools}
        handler_names = set(server.handlers.keys())
        assert tool_names == handler_names
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)


class TestStealthHandlerValidation:
    """Test that handlers validate required args without launching a browser."""

    @pytest.fixture(autouse=True)
    def server(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        self.server = StealthBrowserMCPServer()
        yield
        self.server._loop.call_soon_threadsafe(self.server._loop.stop)
        self.server._thread.join(timeout=2)

    def test_open_missing_url(self):
        result = self.server._handle_open({})
        assert "Error" in result

    def test_goto_missing_url(self):
        result = self.server._handle_goto({})
        assert "Error" in result

    def test_click_missing_ref(self):
        result = self.server._handle_click({})
        assert "Error" in result

    def test_fill_missing_ref(self):
        result = self.server._handle_fill({})
        assert "Error" in result

    def test_fill_missing_value(self):
        result = self.server._handle_fill({"ref": "e1"})
        assert "Error" in result

    def test_select_missing_ref(self):
        result = self.server._handle_select({})
        assert "Error" in result

    def test_select_missing_value(self):
        result = self.server._handle_select({"ref": "e1"})
        assert "Error" in result

    def test_type_missing_text(self):
        result = self.server._handle_type({})
        assert "Error" in result

    def test_press_missing_key(self):
        result = self.server._handle_press({})
        assert "Error" in result

    def test_evaluate_missing_js(self):
        result = self.server._handle_evaluate({})
        assert "Error" in result

    def test_screenshot_missing_path(self):
        result = self.server._handle_screenshot({})
        assert "Error" in result


class TestStealthManagerNoBrowser:
    """Test manager methods return errors when no browser is open."""

    def test_snapshot_no_browser(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        result = asyncio.run(mgr.snapshot())
        assert "Error" in result

    def test_click_no_browser(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        result = asyncio.run(mgr.click("e1"))
        assert "Error" in result

    def test_get_url_no_browser(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        result = asyncio.run(mgr.get_url())
        assert "Error" in result

    def test_goto_no_browser(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        result = asyncio.run(mgr.goto("http://example.com"))
        assert "Error" in result

    def test_cleanup_no_browser(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        # Should not raise
        asyncio.run(mgr.cleanup())

    def test_is_running_false_initially(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        assert mgr.is_running is False


class TestStealthGatewayMode:
    """Test gateway mode dispatch for stealth server."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "1")

    def test_tools_list_returns_single_gateway(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        msg = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        resp = server._handle_message(msg)
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "stealth_browser"
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)

    def test_help_action(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        msg = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {
                "name": "stealth_browser",
                "arguments": {"action": "help"},
            },
        }
        resp = server._handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        assert "open" in text
        assert "snapshot" in text
        assert "click" in text
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)

    def test_unknown_action(self):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        server = StealthBrowserMCPServer()
        msg = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {
                "name": "stealth_browser",
                "arguments": {"action": "nonexistent"},
            },
        }
        resp = server._handle_message(msg)
        assert resp["result"].get("isError", False)
        server._loop.call_soon_threadsafe(server._loop.stop)
        server._thread.join(timeout=2)


class TestNopeCHAResolution:
    """Test NopeCHA extension discovery logic."""

    def test_no_solver_returns_none(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({"captcha_solver": "none"})
        assert mgr._resolve_nopecha() is None

    def test_nopecha_no_vendor_dir(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({"captcha_solver": "nopecha"})
        # vendor/ may or may not exist; should not crash
        result = mgr._resolve_nopecha()
        # Result depends on whether vendor/ exists with nopecha dir
        assert result is None or isinstance(result, str)

    def test_default_config_no_solver(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        assert mgr._resolve_nopecha() is None


class TestStealthConfig:
    """Test _get_stealth_config and config injection."""

    def test_default_config(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        config = mgr._get_stealth_config()
        assert config["canvas_seed"] == 42
        assert config["hardware_concurrency"] == 8
        assert config["screen_width"] == 1920
        assert config["timezone"] == "America/New_York"

    def test_config_with_profile(self):
        from extensions.browser.stealth_server import StealthBrowserManager

        mgr = StealthBrowserManager({})
        mgr._active_profile = {
            "fingerprint": {
                "canvas_seed": 99999,
                "screen_width": 2560,
                "timezone": "Europe/London",
            }
        }
        config = mgr._get_stealth_config()
        assert config["canvas_seed"] == 99999
        assert config["screen_width"] == 2560
        assert config["timezone"] == "Europe/London"
        # Unset values use defaults
        assert config["hardware_concurrency"] == 8


class TestProfileManagement:
    """Test profile CRUD operations."""

    @pytest.fixture(autouse=True)
    def setup_mgr(self, tmp_path):
        from extensions.browser.stealth_server import StealthBrowserManager

        self.mgr = StealthBrowserManager({"profiles_dir": str(tmp_path)})
        self.tmp_path = tmp_path

    def test_create_profile(self):
        result = self.mgr.create_profile("test-profile")
        assert "created" in result
        profile_path = self.tmp_path / "test-profile" / "profile.json"
        assert profile_path.exists()
        data = json.loads(profile_path.read_text())
        assert data["name"] == "test-profile"
        assert "canvas_seed" in data["fingerprint"]

    def test_create_profile_deterministic(self):
        self.mgr.create_profile("alpha")
        p1 = json.loads((self.tmp_path / "alpha" / "profile.json").read_text())
        # Delete and recreate
        import shutil

        shutil.rmtree(self.tmp_path / "alpha")
        self.mgr.create_profile("alpha")
        p2 = json.loads((self.tmp_path / "alpha" / "profile.json").read_text())
        assert p1["fingerprint"] == p2["fingerprint"]

    def test_create_profile_with_overrides(self):
        result = self.mgr.create_profile(
            "custom", overrides={"canvas_seed": 12345, "timezone": "Asia/Tokyo"}
        )
        assert "created" in result
        data = json.loads((self.tmp_path / "custom" / "profile.json").read_text())
        assert data["fingerprint"]["canvas_seed"] == 12345
        assert data["fingerprint"]["timezone"] == "Asia/Tokyo"

    def test_create_profile_rejects_proxy_credentials(self):
        result = self.mgr.create_profile(
            "bad-proxy", overrides={"proxy_server": "http://user:pass@host:8080"}
        )
        assert "Error" in result

    def test_create_profile_allows_clean_proxy(self):
        result = self.mgr.create_profile(
            "good-proxy", overrides={"proxy_server": "socks5://host:1080"}
        )
        assert "created" in result
        data = json.loads((self.tmp_path / "good-proxy" / "profile.json").read_text())
        assert data["proxy_server"] == "socks5://host:1080"

    def test_list_profiles_empty(self):
        assert self.mgr.list_profiles() == []

    def test_list_profiles(self):
        self.mgr.create_profile("alpha")
        self.mgr.create_profile("beta")
        profiles = self.mgr.list_profiles()
        assert profiles == ["alpha", "beta"]

    def test_delete_profile(self):
        self.mgr.create_profile("to-delete")
        assert (self.tmp_path / "to-delete" / "profile.json").exists()
        result = self.mgr.delete_profile("to-delete")
        assert "deleted" in result
        assert not (self.tmp_path / "to-delete").exists()

    def test_delete_nonexistent_profile(self):
        result = self.mgr.delete_profile("nonexistent")
        assert "Error" in result

    def test_load_profile(self):
        self.mgr.create_profile("loadme")
        loaded = self.mgr._load_profile("loadme")
        assert loaded is not None
        assert loaded["name"] == "loadme"

    def test_load_nonexistent_profile(self):
        assert self.mgr._load_profile("nope") is None

    def test_invalid_profile_name(self):
        result = self.mgr.create_profile("../../etc/passwd")
        # Name gets sanitized to "etcpasswd"
        assert "created" in result

    def test_empty_profile_name(self):
        result = self.mgr.create_profile("")
        assert "Error" in result


class TestProfileMCPHandlers:
    """Test MCP handler wrappers for profile management."""

    @pytest.fixture(autouse=True)
    def server(self, tmp_path):
        from extensions.browser.stealth_server import StealthBrowserMCPServer

        self.server = StealthBrowserMCPServer()
        # Override profiles dir to tmp
        self.server._manager._profiles_base = tmp_path
        self.tmp_path = tmp_path
        yield
        self.server._loop.call_soon_threadsafe(self.server._loop.stop)
        self.server._thread.join(timeout=2)

    def test_create_profile_handler(self):
        result = self.server._handle_create_profile({"name": "test-prof"})
        assert "created" in result

    def test_create_profile_missing_name(self):
        result = self.server._handle_create_profile({})
        assert "Error" in result

    def test_list_profiles_handler(self):
        self.server._handle_create_profile({"name": "a"})
        self.server._handle_create_profile({"name": "b"})
        result = self.server._handle_list_profiles({})
        assert "a" in result
        assert "b" in result

    def test_list_profiles_empty(self):
        result = self.server._handle_list_profiles({})
        assert "No profiles" in result

    def test_delete_profile_handler(self):
        self.server._handle_create_profile({"name": "del-me"})
        result = self.server._handle_delete_profile({"name": "del-me"})
        assert "deleted" in result

    def test_delete_profile_missing_name(self):
        result = self.server._handle_delete_profile({})
        assert "Error" in result

    def test_check_email_missing_message_id(self):
        result = self.server._handle_read_email({})
        assert "Error" in result


# ---------------------------------------------------------------------------
# Verification extraction tests
# ---------------------------------------------------------------------------


class TestExtractVerification:
    def test_code_after_keyword(self):
        from extensions.browser.extension import _extract_verification

        body = "Your verification code is 482910. Please enter it."
        result = _extract_verification(body)
        assert "482910" in result["codes"]

    def test_otp_keyword(self):
        from extensions.browser.extension import _extract_verification

        body = "Your OTP: 7291"
        result = _extract_verification(body)
        assert "7291" in result["codes"]

    def test_fallback_standalone_digits(self):
        from extensions.browser.extension import _extract_verification

        body = "Enter 83721 to continue"
        result = _extract_verification(body)
        assert "83721" in result["codes"]

    def test_filters_years(self):
        from extensions.browser.extension import _extract_verification

        body = "Copyright 2025. Enter 83721 to continue"
        result = _extract_verification(body)
        assert "2025" not in result["codes"]
        assert "83721" in result["codes"]

    def test_verification_links(self):
        from extensions.browser.extension import _extract_verification

        body = (
            "Click here: https://example.com/verify?token=abc123 or visit https://example.com/home"
        )
        result = _extract_verification(body)
        assert len(result["links"]) == 1
        assert "verify" in result["links"][0]

    def test_confirm_link(self):
        from extensions.browser.extension import _extract_verification

        body = "Please confirm: https://example.com/confirm/email/abc"
        result = _extract_verification(body)
        assert len(result["links"]) == 1

    def test_no_codes_or_links(self):
        from extensions.browser.extension import _extract_verification

        body = "Hello, welcome to our service."
        result = _extract_verification(body)
        assert result["codes"] == []
        assert result["links"] == []
