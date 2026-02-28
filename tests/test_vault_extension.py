"""Tests for vault extension bridge handler logic."""

import asyncio
import pytest
from pathlib import Path

from extensions.vault.extension import ExtensionImpl
from extensions.vault.store import VaultStore


def _run(coro):
    """Run an async function synchronously (no pytest-asyncio needed)."""
    return asyncio.run(coro)


@pytest.fixture
def vault_store(tmp_path):
    return VaultStore(tmp_path / "vault", passphrase="test-pw")


@pytest.fixture
def extension(vault_store):
    ext = ExtensionImpl()
    ext._vault = vault_store
    return ext


class TestBridgeHandler:
    def test_ignores_non_vault_methods(self, extension):
        result = _run(extension._bridge_handler("ask_user", {}))
        assert result is None

    def test_vault_store_via_bridge(self, extension):
        result = _run(extension._bridge_handler("vault_store", {
            "key": "mykey", "value": "myval", "tags": ["test"],
        }))
        assert result == {"ok": True}
        assert extension._vault.get("mykey") == "myval"

    def test_vault_list_via_bridge(self, extension):
        extension._vault.put("a", "1", tags=["x"])
        extension._vault.put("b", "2")

        result = _run(extension._bridge_handler("vault_list", {}))
        keys = result["keys"]
        assert len(keys) == 2

    def test_vault_list_with_tag(self, extension):
        extension._vault.put("a", "1", tags=["email"])
        extension._vault.put("b", "2", tags=["api"])

        result = _run(extension._bridge_handler("vault_list", {"tag": "email"}))
        keys = result["keys"]
        assert len(keys) == 1
        assert keys[0]["key"] == "a"

    def test_vault_retrieve_via_bridge(self, extension):
        extension._vault.put("key", "secret-value")
        result = _run(extension._bridge_handler("vault_retrieve", {"key": "key"}))
        assert result == {"value": "secret-value"}

    def test_vault_retrieve_missing(self, extension):
        result = _run(extension._bridge_handler("vault_retrieve", {"key": "nope"}))
        assert result == {"value": None}

    def test_vault_delete_via_bridge(self, extension):
        extension._vault.put("key", "val")
        result = _run(extension._bridge_handler("vault_delete", {"key": "key"}))
        assert result == {"deleted": True}
        assert extension._vault.get("key") is None

    def test_vault_delete_missing(self, extension):
        result = _run(extension._bridge_handler("vault_delete", {"key": "nope"}))
        assert result == {"deleted": False}

    def test_unknown_vault_method(self, extension):
        result = _run(extension._bridge_handler("vault_unknown", {}))
        assert "error" in result

    def test_vault_not_initialized(self, extension):
        extension._vault = None
        result = _run(extension._bridge_handler("vault_store", {
            "key": "k", "value": "v",
        }))
        assert "error" in result
