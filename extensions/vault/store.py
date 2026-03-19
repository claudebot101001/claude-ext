"""Encrypted credential vault backed by Fernet symmetric encryption.

Key derivation: passphrase → PBKDF2-HMAC-SHA256 (600K iterations) + random salt → Fernet key.
Storage: JSON blob encrypted with Fernet, written atomically with flock.
Metadata: cleartext JSON (key_metadata.json) stores tier + owner_mcp per key.
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
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16

# Tier constants (duplicated from protocol.py to avoid import dependency)
TIER_WEAK = "weak"
TIER_STRONG = "strong"
VALID_TIERS = {TIER_WEAK, TIER_STRONG}

# Default owner_mcp inference from key prefix
_DEFAULT_OWNER_RULES = {
    "crypto/": "crypto",
    "browser/": "browser",
    "memory/": "memory",
    "x/": "browser",
}


def _infer_owner_mcp(key: str) -> str:
    """Infer default owner_mcp from key prefix."""
    for prefix, owner in _DEFAULT_OWNER_RULES.items():
        if key.startswith(prefix):
            return owner
    return "vault"


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
        self._meta_path = self.vault_dir / "key_metadata.json"
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

        # Ensure metadata file exists
        self._ensure_metadata()

        log.info("VaultStore initialized at %s", self.vault_dir)

    # -- metadata management ------------------------------------------------

    def _ensure_metadata(self) -> None:
        """Create key_metadata.json if it doesn't exist, migrating from existing keys."""
        if self._meta_path.exists():
            return
        # Auto-generate metadata for existing keys
        try:
            with self._shared_lock():
                secrets = self._decrypt_file()
        except (ValueError, FileNotFoundError):
            secrets = {}

        meta = {}
        for key in secrets:
            meta[key] = {
                "tier": TIER_WEAK,
                "owner_mcp": _infer_owner_mcp(key),
            }
        self._write_metadata(meta)
        if meta:
            log.info("Migrated metadata for %d existing keys", len(meta))

    def _read_metadata(self) -> dict:
        """Read key_metadata.json. Returns empty dict if not found."""
        if not self._meta_path.exists():
            return {}
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt key_metadata.json, returning empty")
            return {}

    def _write_metadata(self, meta: dict) -> None:
        """Atomically write key_metadata.json."""
        tmp = self._meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.rename(self._meta_path)

    def get_metadata(self, key: str) -> dict | None:
        """Get metadata for a key. Returns {"tier": ..., "owner_mcp": ...} or None."""
        with self._shared_lock():
            meta = self._read_metadata()
        return meta.get(key)

    def set_metadata(self, key: str, tier: str | None = None, owner_mcp: str | None = None) -> dict:
        """Update metadata for a key. Returns the updated metadata entry.

        Only updates provided fields; leaves others unchanged.
        Creates metadata entry if key exists in secrets but not in metadata.
        """
        with self._exclusive_lock():
            meta = self._read_metadata()
            entry = meta.get(key, {"tier": TIER_WEAK, "owner_mcp": _infer_owner_mcp(key)})
            if tier is not None:
                if tier not in VALID_TIERS:
                    raise ValueError(f"Invalid tier: {tier}. Must be one of {VALID_TIERS}")
                entry["tier"] = tier
            if owner_mcp is not None:
                entry["owner_mcp"] = owner_mcp
            meta[key] = entry
            self._write_metadata(meta)
        return entry

    def get_tier(self, key: str) -> str:
        """Get tier for a key. Returns 'weak' if no metadata exists."""
        with self._shared_lock():
            meta = self._read_metadata()
        entry = meta.get(key)
        return entry["tier"] if entry else TIER_WEAK

    # -- public API ---------------------------------------------------------

    def put(
        self,
        key: str,
        value: str,
        tags: list[str] | None = None,
        tier: str | None = None,
        owner_mcp: str | None = None,
    ) -> None:
        """Store a secret. Overwrites if key already exists.

        Also creates/updates metadata entry with tier and owner_mcp.
        """
        with self._exclusive_lock():
            secrets = self._decrypt_file()
            secrets[key] = {"value": value, "tags": tags or []}
            self._encrypt_and_write(secrets)

            # Update metadata
            meta = self._read_metadata()
            existing = meta.get(key, {})
            meta[key] = {
                "tier": tier if tier and tier in VALID_TIERS else existing.get("tier", TIER_WEAK),
                "owner_mcp": owner_mcp or existing.get("owner_mcp", _infer_owner_mcp(key)),
            }
            self._write_metadata(meta)
        log.info("Vault: stored key '%s' (tier=%s)", key, meta[key]["tier"])

    def get(self, key: str) -> str | None:
        """Retrieve a secret value. Returns None if not found."""
        with self._shared_lock():
            secrets = self._decrypt_file()
        entry = secrets.get(key)
        return entry["value"] if entry else None

    def delete(self, key: str) -> bool:
        """Delete a secret and its metadata. Returns True if it existed."""
        with self._exclusive_lock():
            secrets = self._decrypt_file()
            if key not in secrets:
                return False
            del secrets[key]
            self._encrypt_and_write(secrets)

            # Remove metadata
            meta = self._read_metadata()
            meta.pop(key, None)
            self._write_metadata(meta)
        log.info("Vault: deleted key '%s'", key)
        return True

    def list_keys(self, tag: str | None = None) -> list[dict]:
        """List stored keys with their tags, tier, and owner_mcp (not values).

        Returns list of {"key": str, "tags": list[str], "tier": str, "owner_mcp": str}.
        If tag is specified, filter to entries containing that tag.
        """
        with self._shared_lock():
            secrets = self._decrypt_file()
            meta = self._read_metadata()
        result = []
        for k, entry in secrets.items():
            entry_tags = entry.get("tags", [])
            if tag and tag not in entry_tags:
                continue
            key_meta = meta.get(k, {})
            result.append(
                {
                    "key": k,
                    "tags": entry_tags,
                    "tier": key_meta.get("tier", TIER_WEAK),
                    "owner_mcp": key_meta.get("owner_mcp", _infer_owner_mcp(k)),
                }
            )
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
        except InvalidToken as e:
            raise ValueError("Failed to decrypt vault. Wrong passphrase or corrupted data.") from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Vault data corrupted: {e}") from e

    def _encrypt_and_write(self, secrets: dict) -> None:
        """Encrypt and atomically write secrets file. Caller must hold LOCK_EX."""
        plaintext = json.dumps(secrets, indent=2).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        tmp = self._secrets_path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        os.chmod(tmp, 0o600)
        tmp.rename(self._secrets_path)
