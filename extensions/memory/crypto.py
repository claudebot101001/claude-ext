"""Personality file encryption using Fernet symmetric encryption.

Encrypts/decrypts the personality.md content using a key stored in Vault.
The key never touches disk outside Vault's encrypted store.
"""

from cryptography.fernet import Fernet, InvalidToken


def encrypt_personality(plaintext: str, key: str) -> bytes:
    """Encrypt personality content. Returns Fernet token (bytes)."""
    f = Fernet(key.encode("ascii"))
    return f.encrypt(plaintext.encode("utf-8"))


def decrypt_personality(ciphertext: bytes, key: str) -> str:
    """Decrypt personality content. Raises ValueError on bad key."""
    f = Fernet(key.encode("ascii"))
    try:
        return f.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Failed to decrypt personality. Key may have changed.") from e


def generate_key() -> str:
    """Generate a new Fernet key. Returns URL-safe base64 string."""
    return Fernet.generate_key().decode("ascii")
