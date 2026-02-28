"""Tests for MCP tool registry introspection (P1)."""

from pathlib import Path

import pytest

from core.session import SessionManager


@pytest.fixture
def sm(tmp_path):
    return SessionManager(
        base_dir=tmp_path,
        engine_config={"permission_mode": "bypassPermissions"},
    )


class TestRegisterMCPServer:
    def test_register_without_tools(self, sm):
        sm.register_mcp_server("test", {"command": "python", "args": ["s.py"]})
        result = sm.list_mcp_tools()
        assert "test" in result
        assert result["test"] == []  # no tools declared

    def test_register_with_tools(self, sm):
        sm.register_mcp_server("vault", {"command": "python"}, tools=[
            {"name": "vault_store", "description": "Store a secret"},
            {"name": "vault_list", "description": "List keys"},
        ])
        result = sm.list_mcp_tools()
        assert len(result["vault"]) == 2
        assert result["vault"][0]["name"] == "vault_store"
        assert result["vault"][1]["description"] == "List keys"

    def test_tools_missing_description(self, sm):
        sm.register_mcp_server("x", {"command": "python"}, tools=[
            {"name": "no_desc"},
        ])
        result = sm.list_mcp_tools()
        assert result["x"][0]["description"] == ""

    def test_multiple_servers(self, sm):
        sm.register_mcp_server("a", {"command": "python"}, tools=[
            {"name": "a_tool", "description": "A"},
        ])
        sm.register_mcp_server("b", {"command": "python"}, tools=[
            {"name": "b1", "description": "B1"},
            {"name": "b2", "description": "B2"},
        ])
        sm.register_mcp_server("c", {"command": "python"})  # no tools
        result = sm.list_mcp_tools()
        assert len(result) == 3
        assert len(result["a"]) == 1
        assert len(result["b"]) == 2
        assert result["c"] == []

    def test_list_returns_copies(self, sm):
        """Returned lists should be copies, not references to internal state."""
        sm.register_mcp_server("x", {"command": "python"}, tools=[
            {"name": "t1", "description": "d1"},
        ])
        result1 = sm.list_mcp_tools()
        result1["x"].append({"name": "injected"})
        result2 = sm.list_mcp_tools()
        assert len(result2["x"]) == 1  # should not be affected
