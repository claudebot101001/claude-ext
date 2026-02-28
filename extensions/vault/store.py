"""Encrypted credential vault backed by Fernet symmetric encryption.

Key derivation: passphrase → PBKDF2-HMAC-SHA256 (600K iterations) + random salt → Fernet key.
Storage: JSON blob encrypted with Fernet, written atomically with flock.
File permissions: 0600 on all sensitive files.

Thread/process safety: flock on the encrypted file for concurrent access.
"""

import base64
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
        store.put("smtp_password", "hunter2", tags=["email"])
        print(store.get("smtp_password"))  # "hunter2"
        store.delete("smtp_password")
    """

    def __init__(self, vault_dir: Path, passphrase: str):
        self.vault_dir = vault_dir
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        self._secrets_path = self.vault_dir / "secrets.json.enc"
        self._salt_path = self.vault_dir / "salt"

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
        secrets = self._read()
        secrets[key] = {"value": value, "tags": tags or []}
        self._write(secrets)
        log.info("Vault: stored key '%s'", key)

    def get(self, key: str) -> str | None:
        """Retrieve a secret value. Returns None if not found."""
        secrets = self._read()
        entry = secrets.get(key)
        return entry["value"] if entry else None

    def delete(self, key: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        secrets = self._read()
        if key not in secrets:
            return False
        del secrets[key]
        self._write(secrets)
        log.info("Vault: deleted key '%s'", key)
        return True

    def list_keys(self, tag: str | None = None) -> list[dict]:
        """List stored keys with their tags (not values).

        Returns list of {"key": str, "tags": list[str]}.
        If tag is specified, filter to entries containing that tag.
        """
        secrets = self._read()
        result = []
        for k, entry in secrets.items():
            entry_tags = entry.get("tags", [])
            if tag and tag not in entry_tags:
                continue
            result.append({"key": k, "tags": entry_tags})
        return result

    def has(self, key: str) -> bool:
        """Check if a key exists without reading its value."""
        secrets = self._read()
        return key in secrets

    # -- internal I/O -------------------------------------------------------

    def _read(self) -> dict:
        """Read and decrypt the secrets file. Returns empty dict if missing."""
        if not self._secrets_path.exists():
            return {}

        with open(self._secrets_path, "rb") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                encrypted = f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

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

    def _write(self, secrets: dict) -> None:
        """Encrypt and atomically write the secrets file."""
        plaintext = json.dumps(secrets, indent=2).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)

        tmp = self._secrets_path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(encrypted)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.chmod(tmp, 0o600)
        tmp.rename(self._secrets_path)
