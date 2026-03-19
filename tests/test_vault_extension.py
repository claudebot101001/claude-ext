"""Tests for vault extension bridge handler logic."""

import asyncio
from unittest.mock import MagicMock

import pytest

from extensions.vault.extension import ExtensionImpl
from extensions.vault.store import VaultStore


def _run(coro):
    """Run an async function synchronously (no pytest-asyncio needed)."""
    return asyncio.run(coro)


@pytest.fixture
def vault_store(tmp_path):
    return VaultStore(tmp_path / "vault", passphrase="test-pw")


class _StubEngine:
    events = None


@pytest.fixture
def extension(vault_store):
    ext = ExtensionImpl()
    ext.engine = _StubEngine()
    ext._vault = vault_store
    return ext


# -- Bridge handler tests --


class TestVaultBridgeStore:
    def test_store_basic(self, extension):
        result = _run(
            extension._bridge_handler(
                "vault_store",
                {"key": "api/github/token", "value": "ghp_test123", "session_id": "s1"},
            )
        )
        assert result == {"ok": True}
        assert extension._vault.get("api/github/token") == "ghp_test123"

    def test_store_with_tags(self, extension):
        _run(
            extension._bridge_handler(
                "vault_store",
                {
                    "key": "api/openai/key",
                    "value": "sk-xxx",
                    "tags": ["ai", "production"],
                    "session_id": "s1",
                },
            )
        )
        keys = extension._vault.list_keys()
        assert any(k["key"] == "api/openai/key" for k in keys)

    def test_store_invalid_key(self, extension):
        result = _run(
            extension._bridge_handler(
                "vault_store",
                {"key": "badkey", "value": "val", "session_id": "s1"},
            )
        )
        assert "error" in result

    def test_store_empty_value(self, extension):
        result = _run(
            extension._bridge_handler(
                "vault_store",
                {"key": "api/test/key", "value": "", "session_id": "s1"},
            )
        )
        assert "error" in result


class TestVaultBridgeList:
    def test_list_empty(self, extension):
        result = _run(extension._bridge_handler("vault_list", {"session_id": "s1"}))
        assert result == {"keys": []}

    def test_list_after_store(self, extension):
        extension._vault.put("api/test/key", "val")
        result = _run(extension._bridge_handler("vault_list", {"session_id": "s1"}))
        assert len(result["keys"]) == 1
        assert result["keys"][0]["key"] == "api/test/key"


class TestVaultBridgeDelete:
    def test_delete_existing(self, extension):
        extension._vault.put("api/test/key", "val")
        result = _run(
            extension._bridge_handler(
                "vault_delete",
                {"key": "api/test/key", "session_id": "s1"},
            )
        )
        assert result == {"deleted": True}
        assert extension._vault.get("api/test/key") is None

    def test_delete_nonexistent(self, extension):
        result = _run(
            extension._bridge_handler(
                "vault_delete",
                {"key": "api/nonexistent/key", "session_id": "s1"},
            )
        )
        assert result == {"deleted": False}


class TestVaultBridgeRetrieve:
    def test_retrieve_disabled(self, extension):
        result = _run(
            extension._bridge_handler(
                "vault_retrieve",
                {"key": "api/test/key", "session_id": "s1"},
            )
        )
        assert "error" in result
        assert "disabled" in result["error"].lower()


class TestVaultBridgeRouting:
    def test_non_vault_method_returns_none(self, extension):
        result = _run(extension._bridge_handler("memory_read", {"path": "test.md"}))
        assert result is None

    def test_unknown_vault_method(self, extension):
        result = _run(extension._bridge_handler("vault_unknown", {"session_id": "s1"}))
        assert "error" in result
