"""Encrypted credential vault backed by Fernet symmetric encryption.

Key derivation: passphrase → PBKDF2-HMAC-SHA256 (600K iterations) + random salt → Fernet key.
Storage: JSON blob encrypted with Fernet, written atomically with flock.
File permissions: 0700 on vault directory, 0600 on all files.

Thread/process safety: unified lockfile (secrets.lock) ensures read/write mutual
exclusion.  Read-only ops take LOCK_SH; mutations hold LOCK_EX across the full
read-modify-write cycle to prevent lost updates.
"""

import base64
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

log = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 32-byte Fernet key from passphrase + salt via PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class VaultStore:
    """Encrypted key-value store for sensitive credentials.

    Usage::

        store = VaultStore(Path("~/.claude-ext/vault"), passphrase="my-secret")
        store.put("email/smtp/password", "hunter2", tags=["email"])
        print(store.get("email/smtp/password"))  # "hunter2"
        store.delete("email/smtp/password")
    """

    def __init__(self, vault_dir: Path, passphrase: str):
        self.vault_dir = vault_dir
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.vault_dir, 0o700)

        self._secrets_path = self.vault_dir / "secrets.json.enc"
        self._salt_path = self.vault_dir / "salt"
        self._lock_path = self.vault_dir / "secrets.lock"

        # Load or generate salt
        if self._salt_path.exists():
            self._salt = self._salt_path.read_bytes()
        else:
            self._salt = os.urandom(_SALT_BYTES)
            self._salt_path.write_bytes(self._salt)
            os.chmod(self._salt_path, 0o600)

        self._fernet = Fernet(_derive_key(passphrase, self._salt))
        log.info("VaultStore initialized at %s", self.vault_dir)

    # -- public API ---------------------------------------------------------

    def put(self, key: str, value: str, tags: list[str] | None = None) -> None:
        """Store a secret. Overwrites if key already exists."""
        with self._exclusive_lock():
            secrets = self._decrypt_file()
            secrets[key] = {"value": value, "tags": tags or []}
            self._encrypt_and_write(secrets)
        log.info("Vault: stored key '%s'", key)

    def get(self, key: str) -> str | None:
        """Retrieve a secret value. Returns None if not found."""
        with self._shared_lock():
            secrets = self._decrypt_file()
        entry = secrets.get(key)
        return entry["value"] if entry else None

    def delete(self, key: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        with self._exclusive_lock():
            secrets = self._decrypt_file()
            if key not in secrets:
                return False
            del secrets[key]
            self._encrypt_and_write(secrets)
        log.info("Vault: deleted key '%s'", key)
        return True

    def list_keys(self, tag: str | None = None) -> list[dict]:
        """List stored keys with their tags (not values).

        Returns list of {"key": str, "tags": list[str]}.
        If tag is specified, filter to entries containing that tag.
        """
        with self._shared_lock():
            secrets = self._decrypt_file()
        result = []
        for k, entry in secrets.items():
            entry_tags = entry.get("tags", [])
            if tag and tag not in entry_tags:
                continue
            result.append({"key": k, "tags": entry_tags})
        return result

    def has(self, key: str) -> bool:
        """Check if a key exists without reading its value."""
        with self._shared_lock():
            secrets = self._decrypt_file()
        return key in secrets

    # -- locking ------------------------------------------------------------

    @contextlib.contextmanager
    def _shared_lock(self):
        """LOCK_SH on the unified lockfile."""
        f = open(self._lock_path, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    @contextlib.contextmanager
    def _exclusive_lock(self):
        """LOCK_EX on the unified lockfile."""
        f = open(self._lock_path, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    # -- internal I/O (caller must hold appropriate lock) -------------------

    def _decrypt_file(self) -> dict:
        """Read and decrypt secrets file. Caller must hold lock."""
        if not self._secrets_path.exists():
            return {}
        encrypted = self._secrets_path.read_bytes()
        if not encrypted:
            return {}
        try:
            decrypted = self._fernet.decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except InvalidToken:
            raise ValueError(
                "Failed to decrypt vault. Wrong passphrase or corrupted data."
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Vault data corrupted: {e}")

    def _encrypt_and_write(self, secrets: dict) -> None:
        """Encrypt and atomically write secrets file. Caller must hold LOCK_EX."""
        plaintext = json.dumps(secrets, indent=2).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        tmp = self._secrets_path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        os.chmod(tmp, 0o600)
        tmp.rename(self._secrets_path)
