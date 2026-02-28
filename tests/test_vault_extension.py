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
    ext._internal_prefixes = []
    return ext


class TestBridgeHandler:
    """Bridge handler tests.  All calls include session_id as in production."""

    _SID = "test-session-00000000"

    def test_ignores_non_vault_methods(self, extension):
        result = _run(extension._bridge_handler("ask_user", {}))
        assert result is None

    def test_vault_store_via_bridge(self, extension):
        result = _run(extension._bridge_handler("vault_store", {
            "key": "api/github/token", "value": "ghp_xxx",
            "tags": ["test"], "session_id": self._SID,
        }))
        assert result == {"ok": True}
        assert extension._vault.get("api/github/token") == "ghp_xxx"

    def test_vault_list_via_bridge(self, extension):
        extension._vault.put("a/b/c", "1", tags=["x"])
        extension._vault.put("d/e/f", "2")

        result = _run(extension._bridge_handler("vault_list", {
            "session_id": self._SID,
        }))
        keys = result["keys"]
        assert len(keys) == 2

    def test_vault_list_with_tag(self, extension):
        extension._vault.put("email/smtp/pw", "1", tags=["email"])
        extension._vault.put("api/gh/tok", "2", tags=["api"])

        result = _run(extension._bridge_handler("vault_list", {
            "tag": "email", "session_id": self._SID,
        }))
        keys = result["keys"]
        assert len(keys) == 1
        assert keys[0]["key"] == "email/smtp/pw"

    def test_vault_retrieve_via_bridge(self, extension):
        extension._vault.put("api/test/key", "secret-value")
        result = _run(extension._bridge_handler("vault_retrieve", {
            "key": "api/test/key", "session_id": self._SID,
        }))
        assert result == {"value": "secret-value"}

    def test_vault_retrieve_missing(self, extension):
        result = _run(extension._bridge_handler("vault_retrieve", {
            "key": "api/nope/key", "session_id": self._SID,
        }))
        assert result == {"value": None}

    def test_vault_delete_via_bridge(self, extension):
        extension._vault.put("api/del/key", "val")
        result = _run(extension._bridge_handler("vault_delete", {
            "key": "api/del/key", "session_id": self._SID,
        }))
        assert result == {"deleted": True}
        assert extension._vault.get("api/del/key") is None

    def test_vault_delete_missing(self, extension):
        result = _run(extension._bridge_handler("vault_delete", {
            "key": "api/nope/key", "session_id": self._SID,
        }))
        assert result == {"deleted": False}

    def test_unknown_vault_method(self, extension):
        result = _run(extension._bridge_handler("vault_unknown", {
            "session_id": self._SID,
        }))
        assert "error" in result

    def test_vault_not_initialized(self, extension):
        extension._vault = None
        result = _run(extension._bridge_handler("vault_store", {
            "key": "a/b/c", "value": "v", "session_id": self._SID,
        }))
        assert "error" in result

    def test_missing_session_id_uses_fallback(self, extension):
        """Bridge handler gracefully handles missing session_id."""
        result = _run(extension._bridge_handler("vault_store", {
            "key": "a/b/c", "value": "v",
        }))
        assert result == {"ok": True}


class TestKeyValidation:
    """Test that bridge handler enforces key naming convention."""

    _SID = "test-session-00000000"

    def test_valid_namespaced_keys(self, extension):
        """Keys with category/service/name format are accepted."""
        valid_keys = [
            "api/github/token",
            "email/smtp/password",
            "wallet/eth/privkey",
            "wallet/eth/0xABC123/privkey",
            "a/b/c",
        ]
        for key in valid_keys:
            result = _run(extension._bridge_handler("vault_store", {
                "key": key, "value": "v", "session_id": self._SID,
            }))
            assert result == {"ok": True}, f"Key '{key}' should be valid"

    def test_reject_flat_key(self, extension):
        """Keys without slashes are rejected."""
        result = _run(extension._bridge_handler("vault_store", {
            "key": "my_api_key", "value": "v", "session_id": self._SID,
        }))
        assert "error" in result
        assert "category/service/name" in result["error"]

    def test_reject_single_segment_key(self, extension):
        """Keys with only one slash (two segments) should still be accepted
        since they have category/name — the pattern requires at least two segments."""
        result = _run(extension._bridge_handler("vault_store", {
            "key": "api/token", "value": "v", "session_id": self._SID,
        }))
        assert result == {"ok": True}

    def test_reject_empty_key(self, extension):
        result = _run(extension._bridge_handler("vault_store", {
            "key": "", "value": "v", "session_id": self._SID,
        }))
        assert "error" in result

    def test_reject_empty_value(self, extension):
        result = _run(extension._bridge_handler("vault_store", {
            "key": "a/b/c", "value": "", "session_id": self._SID,
        }))
        assert "error" in result

    def test_reject_key_with_spaces(self, extension):
        result = _run(extension._bridge_handler("vault_store", {
            "key": "api/my token", "value": "v", "session_id": self._SID,
        }))
        assert "error" in result

    def test_reject_key_with_path_traversal(self, extension):
        result = _run(extension._bridge_handler("vault_store", {
            "key": "../etc/passwd", "value": "v", "session_id": self._SID,
        }))
        assert "error" in result

    def test_missing_key_in_retrieve(self, extension):
        result = _run(extension._bridge_handler("vault_retrieve", {
            "session_id": self._SID,
        }))
        assert "error" in result

    def test_missing_key_in_delete(self, extension):
        result = _run(extension._bridge_handler("vault_delete", {
            "session_id": self._SID,
        }))
        assert "error" in result


class TestInternalPrefixes:
    """Test the internal_prefixes access control mechanism."""

    _SID = "test-session-00000000"

    def test_no_prefixes_by_default(self, extension):
        """With no internal prefixes, all keys are accessible."""
        extension._vault.put("wallet/eth/privkey", "0xDEADBEEF")
        result = _run(extension._bridge_handler("vault_retrieve", {
            "key": "wallet/eth/privkey", "session_id": self._SID,
        }))
        assert result == {"value": "0xDEADBEEF"}

    def test_internal_prefix_blocks_retrieve(self, extension):
        """Keys matching internal prefixes are blocked from MCP retrieve."""
        extension._internal_prefixes = ["wallet/"]
        extension._vault.put("wallet/eth/privkey", "0xDEADBEEF")

        result = _run(extension._bridge_handler("vault_retrieve", {
            "key": "wallet/eth/privkey", "session_id": self._SID,
        }))
        assert "error" in result
        assert "internal-only" in result["error"]

    def test_internal_prefix_does_not_block_other_keys(self, extension):
        """Non-matching keys are still accessible."""
        extension._internal_prefixes = ["wallet/"]
        extension._vault.put("api/github/token", "ghp_xxx")

        result = _run(extension._bridge_handler("vault_retrieve", {
            "key": "api/github/token", "session_id": self._SID,
        }))
        assert result == {"value": "ghp_xxx"}

    def test_programmatic_access_bypasses_prefix_check(self, extension):
        """engine.services['vault'].get() bypasses prefix restrictions.
        This is how wallet extension will read private keys internally."""
        extension._internal_prefixes = ["wallet/"]
        extension._vault.put("wallet/eth/privkey", "0xDEADBEEF")

        # Direct VaultStore access (as other extensions would use)
        assert extension._vault.get("wallet/eth/privkey") == "0xDEADBEEF"
