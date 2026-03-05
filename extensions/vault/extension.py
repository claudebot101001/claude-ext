"""Vault extension — encrypted credential storage for Claude Code sessions.

Provides:
1. ``engine.services["vault"]`` — VaultStore instance for programmatic access
   by other extensions (e.g. wallet can decrypt private keys without LLM).
2. MCP tools (vault_store/list/retrieve/delete) for Claude to manage secrets.
3. System prompt guidance to prevent echoing secrets to users.

Passphrase priority: CLAUDE_EXT_VAULT_PASSPHRASE env var > .passphrase file
> auto-generate.  The env var is unset in Claude sessions to prevent leakage.

Security note: encryption is defense-in-depth (prevents plaintext secrets in
file copies/backups), NOT a primary security boundary.  The real access control
is ``_internal_prefixes`` (blocks MCP retrieve for sensitive keys) and OS-level
permissions.  In bypassPermissions mode, Claude has full filesystem access
regardless of encryption.
"""

import logging
import os
import re
import sys
from pathlib import Path

from core.extension import Extension
from extensions.vault.store import VaultStore

log = logging.getLogger(__name__)

# Key must be namespaced: category/service/name (at least two slashes).
# Allowed chars: a-z A-Z 0-9 _ - . in each segment.
_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+(/[a-zA-Z0-9._-]+)+$")


class ExtensionImpl(Extension):
    name = "vault"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._vault: VaultStore | None = None
        # Phase 4+: prefixes whose keys can only be read by bridge handlers
        # internally (e.g. wallet extension reads private keys in-process),
        # never returned to LLM via vault_retrieve MCP tool.
        self._internal_prefixes: list[str] = []

    @property
    def sm(self):
        return self.engine.session_manager

    async def start(self) -> None:
        # Initialize vault directory
        state_dir = self.sm.base_dir if self.sm else Path("~/.claude-ext").expanduser()
        vault_dir = state_dir / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)

        # Passphrase: env var > .passphrase file > auto-generate
        passphrase = os.environ.get("CLAUDE_EXT_VAULT_PASSPHRASE", "")
        passphrase_file = vault_dir / ".passphrase"

        if passphrase:
            log.info("Vault: using passphrase from environment variable")
        else:
            if passphrase_file.exists():
                passphrase = passphrase_file.read_text(encoding="utf-8").strip()
            if not passphrase:
                # File missing or empty — generate a new one
                import secrets as _secrets

                passphrase = _secrets.token_urlsafe(32)
                passphrase_file.write_text(passphrase, encoding="utf-8")
                os.chmod(passphrase_file, 0o600)
                log.info("Vault: generated new passphrase at %s", passphrase_file)
            else:
                log.info("Vault: loaded passphrase from %s", passphrase_file)

        self._vault = VaultStore(vault_dir, passphrase)

        # Prevent passphrase env var from leaking into Claude sessions
        self.sm.register_env_unset("CLAUDE_EXT_VAULT_PASSPHRASE")

        # Register as shared service for other extensions
        self.engine.services["vault"] = self._vault
        self.engine.services["vault_ext"] = self

        # Register MCP server for Claude session access
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server(
            "vault",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {},  # No passphrase here — MCP uses bridge RPC
            },
            tools=[
                {
                    "name": "vault_store",
                    "description": "Store a secret (key + value + optional tags)",
                },
                {"name": "vault_list", "description": "List all keys and tags (no values)"},
                {"name": "vault_retrieve", "description": "Retrieve a secret value by key"},
                {"name": "vault_delete", "description": "Delete a secret by key"},
            ],
        )

        # Register bridge handler for MCP → main process calls
        self.engine.bridge.add_handler(self._bridge_handler)

        # System prompt: security constraints only (tool descriptions cover usage)
        self.sm.add_system_prompt(
            "CRITICAL: Never echo or display secret values to the user. "
            "When you retrieve a secret, use it directly in subsequent tool "
            "calls (e.g. as an API key in a curl command, as a password in "
            "a config file). If the user asks to see a secret value, remind "
            "them to check the vault directly for security reasons.",
            mcp_server="vault",
        )

        log.info(
            "Vault extension started. %d secret(s) stored.",
            len(self._vault.list_keys()),
        )

    async def stop(self) -> None:
        self.engine.services.pop("vault", None)
        self.engine.services.pop("vault_ext", None)
        log.info("Vault extension stopped.")

    async def health_check(self) -> dict:
        result: dict = {"status": "ok"}
        if self._vault is None:
            return {"status": "error", "detail": "VaultStore not initialized"}
        result["secrets"] = len(self._vault.list_keys())
        if self._internal_prefixes:
            result["policies"] = {"internal_prefixes": list(self._internal_prefixes)}
        return result

    def _validate_key(self, key: str) -> str | None:
        """Validate key format. Returns error message or None if valid."""
        if not key:
            return "Key is required."
        if not _KEY_PATTERN.match(key):
            return (
                f"Invalid key format: '{key}'. "
                "Keys must use 'category/service/name' format "
                "(a-z, 0-9, ._- in each segment)."
            )
        return None

    def register_internal_prefix(self, prefix: str) -> None:
        """Register a key prefix as internal-only (blocked from MCP retrieve)."""
        if prefix not in self._internal_prefixes:
            self._internal_prefixes.append(prefix)
            log.info("Vault: registered internal prefix '%s'", prefix)

    def _is_internal_key(self, key: str) -> bool:
        """Check if a key is restricted to internal-only access."""
        return any(key.startswith(p) for p in self._internal_prefixes)

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        """Handle vault bridge RPCs from MCP server processes.

        Every call includes ``session_id`` identifying the requesting
        session.  Currently logged for audit; Phase 4+ can use it for
        prefix-based access control via ``_internal_prefixes``.
        """
        if not method.startswith("vault_"):
            return None  # not ours

        if self._vault is None:
            return {"error": "Vault not initialized"}

        session_id = params.get("session_id", "unknown")

        try:
            if method == "vault_store":
                key = params.get("key", "")
                value = params.get("value", "")
                if not value:
                    return {"error": "Value is required."}
                err = self._validate_key(key)
                if err:
                    return {"error": err}
                log.info("vault_store key='%s' by session %s", key, session_id[:8])
                self._vault.put(key, value, params.get("tags"))
                if self.engine.events:
                    self.engine.events.log("vault.store", session_id, {"key": key})
                return {"ok": True}

            elif method == "vault_list":
                return {"keys": self._vault.list_keys(tag=params.get("tag"))}

            elif method == "vault_retrieve":
                key = params.get("key", "")
                if not key:
                    return {"error": "Key is required."}
                if self._is_internal_key(key):
                    return {
                        "error": f"Key '{key}' is internal-only. Use the dedicated extension tools."
                    }
                log.info("vault_retrieve key='%s' by session %s", key, session_id[:8])
                if self.engine.events:
                    self.engine.events.log("vault.retrieve", session_id, {"key": key})
                return {"value": self._vault.get(key)}

            elif method == "vault_delete":
                key = params.get("key", "")
                if not key:
                    return {"error": "Key is required."}
                log.info("vault_delete key='%s' by session %s", key, session_id[:8])
                if self.engine.events:
                    self.engine.events.log("vault.delete", session_id, {"key": key})
                return {"deleted": self._vault.delete(key)}

            else:
                return {"error": f"Unknown vault method: {method}"}

        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("Vault bridge handler error")
            return {"error": f"Internal error: {e}"}
