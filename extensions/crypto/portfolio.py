"""Wallet metadata store — tracks managed wallets (no private keys).

JSON file with flock-based atomic I/O matching vault/store.py pattern.
"""

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class PortfolioStore:
    """Manages wallet metadata in a JSON file."""

    def __init__(self, state_dir: Path):
        self._file = state_dir / "wallets.json"
        self._lock = state_dir / "crypto.lock"
        state_dir.mkdir(parents=True, exist_ok=True)
        if not self._file.exists():
            self._file.write_text("[]", encoding="utf-8")

    def _read(self) -> list[dict]:
        with open(self._lock, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, FileNotFoundError):
                return []
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _write(self, data: list[dict]) -> None:
        with open(self._lock, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                self._file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def add_wallet(self, address: str, chain: str, label: str = "") -> dict:
        """Add a wallet. Returns the wallet entry."""
        wallets = self._read()
        entry = {
            "address": address,
            "chain": chain,
            "label": label,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        wallets.append(entry)
        self._write(wallets)
        return entry

    def list_wallets(self, chain: str | None = None) -> list[dict]:
        """List wallets, optionally filtered by chain."""
        wallets = self._read()
        if chain:
            wallets = [w for w in wallets if w["chain"] == chain]
        return wallets

    def get_wallet(self, address: str) -> dict | None:
        """Get wallet by address (case-insensitive)."""
        addr_lower = address.lower()
        for w in self._read():
            if w["address"].lower() == addr_lower:
                return w
        return None
