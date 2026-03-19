"""Tests for extensions/vault/store.py — VaultStore."""

import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from extensions.vault.store import VaultStore


@pytest.fixture
def vault_dir(tmp_path):
    return tmp_path / "vault"


@pytest.fixture
def store(vault_dir):
    return VaultStore(vault_dir, passphrase="test-passphrase-123")


class TestVaultStoreBasics:
    def test_put_and_get(self, store):
        store.put("api_key", "sk-abc123")
        assert store.get("api_key") == "sk-abc123"

    def test_get_missing_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_put_overwrites(self, store):
        store.put("key", "value1")
        store.put("key", "value2")
        assert store.get("key") == "value2"

    def test_delete(self, store):
        store.put("key", "value")
        assert store.delete("key") is True
        assert store.get("key") is None

    def test_delete_missing_returns_false(self, store):
        assert store.delete("nonexistent") is False

    def test_has(self, store):
        store.put("key", "value")
        assert store.has("key") is True
        assert store.has("nonexistent") is False

    def test_list_keys(self, store):
        store.put("a", "1", tags=["email"])
        store.put("b", "2", tags=["api", "production"])
        store.put("c", "3")

        keys = store.list_keys()
        assert len(keys) == 3
        key_names = {k["key"] for k in keys}
        assert key_names == {"a", "b", "c"}

        # Values are NOT in the listing
        for entry in keys:
            assert "value" not in entry

    def test_list_keys_with_tag_filter(self, store):
        store.put("smtp", "pw1", tags=["email"])
        store.put("imap", "pw2", tags=["email", "read"])
        store.put("github", "tok", tags=["api"])

        email_keys = store.list_keys(tag="email")
        assert len(email_keys) == 2
        assert {k["key"] for k in email_keys} == {"smtp", "imap"}

        api_keys = store.list_keys(tag="api")
        assert len(api_keys) == 1
        assert api_keys[0]["key"] == "github"


class TestVaultStoreEncryption:
    def test_file_is_encrypted_on_disk(self, store, vault_dir):
        store.put("secret", "my-password")
        enc_file = vault_dir / "secrets.json.enc"
        assert enc_file.exists()

        raw = enc_file.read_bytes()
        # The encrypted file should NOT contain the plaintext
        assert b"my-password" not in raw
        assert b"secret" not in raw

    def test_wrong_passphrase_fails(self, vault_dir):
        store1 = VaultStore(vault_dir, passphrase="correct")
        store1.put("key", "value")

        store2 = VaultStore(vault_dir, passphrase="wrong")
        with pytest.raises(ValueError, match="decrypt"):
            store2.get("key")

    def test_same_passphrase_can_read(self, vault_dir):
        store1 = VaultStore(vault_dir, passphrase="same-pass")
        store1.put("key", "value")

        # New instance, same passphrase, reuses existing salt
        store2 = VaultStore(vault_dir, passphrase="same-pass")
        assert store2.get("key") == "value"

    def test_salt_persisted(self, vault_dir):
        store1 = VaultStore(vault_dir, passphrase="test")
        salt1 = store1._salt

        store2 = VaultStore(vault_dir, passphrase="test")
        salt2 = store2._salt

        assert salt1 == salt2

    def test_corrupted_file_raises_valueerror(self, store, vault_dir):
        """Corrupted encrypted file should raise ValueError, not crash."""
        store.put("key", "value")
        enc_path = vault_dir / "secrets.json.enc"
        # Corrupt the file
        enc_path.write_bytes(b"this is not valid fernet data")
        with pytest.raises(ValueError, match=r"decrypt|corrupted"):
            store.get("key")

    def test_truncated_file_raises_valueerror(self, store, vault_dir):
        """Truncated encrypted file should raise ValueError."""
        store.put("key", "value")
        enc_path = vault_dir / "secrets.json.enc"
        # Truncate to half
        data = enc_path.read_bytes()
        enc_path.write_bytes(data[: len(data) // 2])
        with pytest.raises(ValueError, match=r"decrypt|corrupted"):
            store.get("key")


class TestVaultStoreFilePermissions:
    def test_salt_file_permissions(self, store, vault_dir):
        salt_path = vault_dir / "salt"
        assert salt_path.exists()
        mode = stat.S_IMODE(os.stat(salt_path).st_mode)
        assert mode == 0o600

    def test_secrets_file_permissions(self, store, vault_dir):
        store.put("key", "value")
        enc_path = vault_dir / "secrets.json.enc"
        assert enc_path.exists()
        mode = stat.S_IMODE(os.stat(enc_path).st_mode)
        assert mode == 0o600

    def test_vault_dir_permissions(self, vault_dir):
        """Vault directory should be 0700."""
        VaultStore(vault_dir, passphrase="test")
        mode = stat.S_IMODE(os.stat(vault_dir).st_mode)
        assert mode == 0o700

    def test_vault_dir_created(self, vault_dir):
        """VaultStore creates the directory if it doesn't exist."""
        assert not vault_dir.exists()
        VaultStore(vault_dir, passphrase="test")
        assert vault_dir.exists()


class TestVaultStoreConcurrency:
    def test_concurrent_writes_no_data_loss(self, vault_dir):
        """Multiple threads writing different keys — no data loss."""
        store = VaultStore(vault_dir, passphrase="pw")

        def write_key(i):
            store.put(f"key-{i}", f"val-{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_key, range(20)))

        assert len(store.list_keys()) == 20
        # Verify all values are correct
        for i in range(20):
            assert store.get(f"key-{i}") == f"val-{i}"

    def test_concurrent_read_write(self, vault_dir):
        """Reads don't corrupt data during concurrent writes."""
        store = VaultStore(vault_dir, passphrase="pw")
        # Pre-populate
        for i in range(5):
            store.put(f"pre-{i}", f"val-{i}")

        errors = []

        def reader():
            try:
                keys = store.list_keys()
                assert len(keys) >= 5  # at least the pre-populated ones
            except Exception as e:
                errors.append(e)

        def writer(i):
            store.put(f"new-{i}", f"new-val-{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            # Mix reads and writes
            futures = []
            for i in range(10):
                futures.append(pool.submit(writer, i))
                futures.append(pool.submit(reader))
            for f in futures:
                f.result()

        assert not errors
        assert len(store.list_keys()) == 15  # 5 pre + 10 new


class TestVaultStoreEdgeCases:
    def test_empty_vault_list(self, store):
        assert store.list_keys() == []

    def test_empty_vault_get(self, store):
        assert store.get("anything") is None

    def test_unicode_values(self, store):
        store.put("key", "密码🔑")
        assert store.get("key") == "密码🔑"

    def test_large_value(self, store):
        big = "x" * 100_000
        store.put("big", big)
        assert store.get("big") == big

    def test_special_chars_in_key(self, store):
        store.put("my/key.with-special_chars", "value")
        assert store.get("my/key.with-special_chars") == "value"

    def test_multiple_operations_sequence(self, store):
        """Simulate a realistic usage sequence."""
        store.put("a", "1")
        store.put("b", "2")
        store.put("c", "3")
        assert len(store.list_keys()) == 3

        store.delete("b")
        assert len(store.list_keys()) == 2
        assert store.get("b") is None
        assert store.get("a") == "1"

        store.put("a", "updated")
        assert store.get("a") == "updated"
        assert len(store.list_keys()) == 2
