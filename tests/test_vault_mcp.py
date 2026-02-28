"""Tests for vault MCP server tool handlers (unit-level, no actual MCP protocol)."""

import pytest

from extensions.vault.mcp_server import VaultMCPServer


class FakeBridge:
    """Simulate bridge RPC by dispatching directly to a VaultStore."""

    def __init__(self, store):
        self.store = store
        self.last_params = {}  # capture for inspection

    def call(self, method, params, timeout=30):
        self.last_params = params
        if method == "vault_store":
            self.store.put(params["key"], params["value"], params.get("tags"))
            return {"ok": True}
        elif method == "vault_list":
            keys = self.store.list_keys(tag=params.get("tag"))
            return {"keys": keys}
        elif method == "vault_retrieve":
            value = self.store.get(params["key"])
            return {"value": value}
        elif method == "vault_delete":
            deleted = self.store.delete(params["key"])
            return {"deleted": deleted}
        return {"error": f"Unknown method: {method}"}


@pytest.fixture
def vault_store(tmp_path):
    from extensions.vault.store import VaultStore

    return VaultStore(tmp_path / "vault", passphrase="test-pw")


@pytest.fixture
def mcp(vault_store):
    server = VaultMCPServer()
    server._bridge = FakeBridge(vault_store)
    return server


class TestVaultMCPHandlers:
    def test_store_and_retrieve(self, mcp):
        result = mcp.handlers["vault_store"]({"key": "k1", "value": "v1"})
        assert "Stored" in result
        assert "k1" in result

        result = mcp.handlers["vault_retrieve"]({"key": "k1"})
        assert result == "v1"

    def test_retrieve_missing(self, mcp):
        result = mcp.handlers["vault_retrieve"]({"key": "nope"})
        assert "not found" in result

    def test_list_empty(self, mcp):
        result = mcp.handlers["vault_list"]({})
        assert "empty" in result.lower()

    def test_list_with_entries(self, mcp):
        mcp.handlers["vault_store"]({"key": "a", "value": "1", "tags": ["x"]})
        mcp.handlers["vault_store"]({"key": "b", "value": "2"})

        result = mcp.handlers["vault_list"]({})
        assert "2 secret" in result
        assert "a" in result
        assert "b" in result

    def test_list_with_tag_filter(self, mcp):
        mcp.handlers["vault_store"]({"key": "a", "value": "1", "tags": ["email"]})
        mcp.handlers["vault_store"]({"key": "b", "value": "2", "tags": ["api"]})

        result = mcp.handlers["vault_list"]({"tag": "email"})
        assert "1 secret" in result
        assert "a" in result

    def test_delete(self, mcp):
        mcp.handlers["vault_store"]({"key": "k", "value": "v"})
        result = mcp.handlers["vault_delete"]({"key": "k"})
        assert "Deleted" in result

        result = mcp.handlers["vault_retrieve"]({"key": "k"})
        assert "not found" in result

    def test_delete_missing(self, mcp):
        result = mcp.handlers["vault_delete"]({"key": "nope"})
        assert "not found" in result

    def test_store_missing_key(self, mcp):
        result = mcp.handlers["vault_store"]({"key": "", "value": "v"})
        assert "Error" in result

    def test_store_missing_value(self, mcp):
        result = mcp.handlers["vault_store"]({"key": "k", "value": ""})
        assert "Error" in result

    def test_retrieve_missing_key(self, mcp):
        result = mcp.handlers["vault_retrieve"]({"key": ""})
        assert "Error" in result

    def test_delete_missing_key(self, mcp):
        result = mcp.handlers["vault_delete"]({"key": ""})
        assert "Error" in result


class TestVaultMCPSessionId:
    def test_session_id_passed_in_bridge_calls(self, mcp):
        """Verify MCP server injects session_id into every bridge call."""
        # MCP server reads session_id from CLAUDE_EXT_SESSION_ID env var.
        # In tests it's empty string; verify it's present in params.
        mcp.handlers["vault_store"]({"key": "k", "value": "v"})
        assert "session_id" in mcp._bridge.last_params

        mcp.handlers["vault_retrieve"]({"key": "k"})
        assert "session_id" in mcp._bridge.last_params

        mcp.handlers["vault_list"]({})
        assert "session_id" in mcp._bridge.last_params

        mcp.handlers["vault_delete"]({"key": "k"})
        assert "session_id" in mcp._bridge.last_params


class TestVaultMCPNoBridge:
    def test_no_bridge_returns_error(self):
        server = VaultMCPServer()
        server._bridge = None
        result = server.handlers["vault_store"]({"key": "k", "value": "v"})
        assert "Error" in result
