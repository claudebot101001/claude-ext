"""Tests for MCP gateway mode — consolidated tool dispatch."""

import json
import os

import pytest

from core.mcp_base import MCPServerBase


# ---------------------------------------------------------------------------
# Fixtures: test server subclasses
# ---------------------------------------------------------------------------


class MultiToolServer(MCPServerBase):
    """Test server with multiple tools."""

    name = "multi"
    gateway_description = "Multi-tool test server. action='help' for details."
    tools = [
        {
            "name": "multi_alpha",
            "description": "First action.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "A value"},
                },
                "required": ["value"],
            },
        },
        {
            "name": "multi_beta",
            "description": "Second action.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "A count"},
                },
                "required": [],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "multi_alpha": self._handle_alpha,
            "multi_beta": self._handle_beta,
        }

    def _handle_alpha(self, args):
        return f"alpha:{args.get('value', '')}"

    def _handle_beta(self, args):
        return f"beta:{args.get('count', 0)}"


class SingleToolServer(MCPServerBase):
    """Test server with only one tool (should bypass gateway)."""

    name = "single"
    gateway_description = "Should not appear."
    tools = [
        {
            "name": "single_only",
            "description": "The only tool.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "single_only": lambda args: "only-result",
        }


class NoDescServer(MCPServerBase):
    """Multi-tool server without custom gateway_description."""

    name = "nodesc"
    tools = [
        {
            "name": "nodesc_a",
            "description": "Action A.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "nodesc_b",
            "description": "Action B.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "nodesc_a": lambda args: "a-result",
            "nodesc_b": lambda args: "b-result",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": msg_id}
    if params is not None:
        msg["params"] = params
    return msg


def _tool_call(name, arguments, msg_id=1):
    return _make_msg("tools/call", {"name": name, "arguments": arguments}, msg_id)


def _get_text(response):
    """Extract text from a tools/call response."""
    return response["result"]["content"][0]["text"]


def _is_error(response):
    return response["result"].get("isError", False)


# ---------------------------------------------------------------------------
# Gateway OFF — normal behavior (baseline)
# ---------------------------------------------------------------------------


class TestNormalMode:
    """Verify normal (non-gateway) behavior is preserved."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_EXT_GATEWAY_MODE", raising=False)

    def test_tools_list_returns_all_tools(self):
        server = MultiToolServer()
        resp = server._handle_message(_make_msg("tools/list"))
        tools = resp["result"]["tools"]
        assert len(tools) == 2
        assert tools[0]["name"] == "multi_alpha"
        assert tools[1]["name"] == "multi_beta"

    def test_direct_tool_call(self):
        server = MultiToolServer()
        resp = server._handle_message(_tool_call("multi_alpha", {"value": "hello"}))
        assert _get_text(resp) == "alpha:hello"

    def test_unknown_tool(self):
        server = MultiToolServer()
        resp = server._handle_message(_tool_call("nonexistent", {}))
        assert _is_error(resp)
        assert "Unknown tool" in _get_text(resp)


# ---------------------------------------------------------------------------
# Gateway ON — consolidated tool
# ---------------------------------------------------------------------------


class TestGatewayMode:
    """Test gateway mode behavior."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "1")

    def test_tools_list_returns_single_gateway_tool(self):
        server = MultiToolServer()
        resp = server._handle_message(_make_msg("tools/list"))
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "multi"
        assert "action" in tools[0]["inputSchema"]["properties"]
        assert "params" in tools[0]["inputSchema"]["properties"]

    def test_gateway_description_used(self):
        server = MultiToolServer()
        resp = server._handle_message(_make_msg("tools/list"))
        tool = resp["result"]["tools"][0]
        assert tool["description"] == MultiToolServer.gateway_description

    def test_help_action(self):
        server = MultiToolServer()
        resp = server._handle_message(_tool_call("multi", {"action": "help"}))
        text = _get_text(resp)
        assert "multi_alpha" in text
        assert "multi_beta" in text
        assert "value" in text  # parameter name
        assert "(required)" in text

    def test_dispatch_to_handler(self):
        server = MultiToolServer()
        resp = server._handle_message(
            _tool_call("multi", {"action": "multi_alpha", "params": {"value": "test"}})
        )
        assert _get_text(resp) == "alpha:test"

    def test_dispatch_second_handler(self):
        server = MultiToolServer()
        resp = server._handle_message(
            _tool_call("multi", {"action": "multi_beta", "params": {"count": 42}})
        )
        assert _get_text(resp) == "beta:42"

    def test_dispatch_empty_params(self):
        server = MultiToolServer()
        resp = server._handle_message(_tool_call("multi", {"action": "multi_beta"}))
        assert _get_text(resp) == "beta:0"

    def test_unknown_action(self):
        server = MultiToolServer()
        resp = server._handle_message(_tool_call("multi", {"action": "nonexistent"}))
        assert _is_error(resp)
        assert "Unknown action" in _get_text(resp)
        assert "multi_alpha" in _get_text(resp)  # lists available actions

    def test_handler_exception(self):
        server = MultiToolServer()
        # Replace handler with one that raises
        server.handlers["multi_alpha"] = lambda args: (_ for _ in ()).throw(ValueError("boom"))
        resp = server._handle_message(_tool_call("multi", {"action": "multi_alpha", "params": {}}))
        assert "Error" in _get_text(resp)


# ---------------------------------------------------------------------------
# Single-tool server bypass
# ---------------------------------------------------------------------------


class TestSingleToolBypass:
    """Servers with 1 tool should NOT use gateway even when mode is on."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "1")

    def test_tools_list_returns_original(self):
        server = SingleToolServer()
        resp = server._handle_message(_make_msg("tools/list"))
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "single_only"

    def test_direct_call_still_works(self):
        server = SingleToolServer()
        resp = server._handle_message(_tool_call("single_only", {}))
        assert _get_text(resp) == "only-result"


# ---------------------------------------------------------------------------
# Auto-generated description fallback
# ---------------------------------------------------------------------------


class TestAutoDescription:
    """Server without gateway_description gets auto-generated one."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "1")

    def test_fallback_description(self):
        server = NoDescServer()
        resp = server._handle_message(_make_msg("tools/list"))
        tool = resp["result"]["tools"][0]
        assert "nodesc" in tool["description"]
        assert "action='help'" in tool["description"]


# ---------------------------------------------------------------------------
# Help text generation
# ---------------------------------------------------------------------------


class TestHelpGeneration:
    """Test the _generate_help() method."""

    def test_help_includes_all_tools(self):
        server = MultiToolServer()
        help_text = server._generate_help()
        assert "multi_alpha" in help_text
        assert "multi_beta" in help_text

    def test_help_includes_parameters(self):
        server = MultiToolServer()
        help_text = server._generate_help()
        assert "`value`" in help_text
        assert "(required)" in help_text
        assert "`count`" in help_text

    def test_help_includes_descriptions(self):
        server = MultiToolServer()
        help_text = server._generate_help()
        assert "First action." in help_text
        assert "Second action." in help_text


# ---------------------------------------------------------------------------
# _is_gateway_active edge cases
# ---------------------------------------------------------------------------


class TestIsGatewayActive:
    def test_off_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_EXT_GATEWAY_MODE", raising=False)
        server = MultiToolServer()
        assert not server._is_gateway_active()

    def test_off_when_env_not_1(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "0")
        server = MultiToolServer()
        assert not server._is_gateway_active()

    def test_on_for_multi_tool(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "1")
        server = MultiToolServer()
        assert server._is_gateway_active()

    def test_off_for_single_tool(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_EXT_GATEWAY_MODE", "1")
        server = SingleToolServer()
        assert not server._is_gateway_active()
