"""Vault extension — encrypted credential storage for Claude Code sessions.

Demonstrates the Bridge RPC pattern:
- MCP server (child process) exposes tools to Claude
- All operations route through bridge to the main process
- Passphrase and VaultStore never leave the main process memory

Provides:
1. ``engine.services["vault"]`` — VaultStore for programmatic access by other extensions.
2. MCP tools (vault_store/list/delete) for Claude to manage secrets.
   vault_retrieve is disabled — sessions cannot read secret values.
3. System prompt guidance to prevent echoing secrets.
"""

import logging
import os
import re
import sys
from pathlib import Path

from core.extension import Extension
from core.mcp_tags import READ_ONLY_WORKER_EXCLUDE_TAG
from extensions.vault.store import VaultStore

log = logging.getLogger(__name__)

# Key must be namespaced: category/service/name (at least two slashes).
_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+(/[a-zA-Z0-9._-]+)+$")


class ExtensionImpl(Extension):
    name = "vault"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._vault: VaultStore | None = None

    @property
    def sm(self):
        return self.engine.session_manager

    async def start(self) -> None:
        state_dir = self.sm.base_dir if self.sm else Path("~/.claude-ext").expanduser()
        vault_dir = state_dir / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)

        # Passphrase priority: env var > .passphrase file > auto-generate
        passphrase = os.environ.get("CLAUDE_EXT_VAULT_PASSPHRASE", "")
        passphrase_file = vault_dir / ".passphrase"

        if passphrase:
            log.info("Vault: using passphrase from environment variable")
        else:
            if passphrase_file.exists():
                passphrase = passphrase_file.read_text(encoding="utf-8").strip()
            if not passphrase:
                import secrets as _secrets

                passphrase = _secrets.token_urlsafe(32)
                passphrase_file.write_text(passphrase, encoding="utf-8")
                os.chmod(passphrase_file, 0o600)
                log.info("Vault: generated new passphrase at %s", passphrase_file)
            else:
                log.info("Vault: loaded passphrase from %s", passphrase_file)

        self._vault = VaultStore(vault_dir, passphrase)

        # Prevent passphrase from leaking into Claude sessions
        self.sm.register_env_unset("CLAUDE_EXT_VAULT_PASSPHRASE")

        # Register as shared service for other extensions
        self.engine.services["vault"] = self._vault

        # Register MCP server — child process communicates via bridge RPC
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server(
            "vault",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {},  # No passphrase here — MCP uses bridge RPC
            },
            tags=[READ_ONLY_WORKER_EXCLUDE_TAG],
            tools=[
                {
                    "name": "vault_store",
                    "description": "Store a secret (key + value + optional tags)",
                },
                {"name": "vault_list", "description": "List all keys and tags (no values)"},
                {"name": "vault_delete", "description": "Delete a secret by key"},
            ],
        )

        # Register bridge handler for MCP → main process calls
        self.engine.bridge.add_handler(self._bridge_handler)

        # System prompt: security constraints
        self.sm.add_system_prompt(
            "CRITICAL: Never echo or display secret values to the user. "
            "Secret retrieval is disabled — you cannot read secret values from the vault. "
            "If the user asks to see a secret value, remind them to check the command line.",
            mcp_server="vault",
        )

        log.info(
            "Vault extension started. %d secret(s) stored.",
            len(self._vault.list_keys()),
        )

    async def stop(self) -> None:
        self.engine.services.pop("vault", None)
        log.info("Vault extension stopped.")

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

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        """Handle vault bridge RPCs from MCP server processes."""
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
                return {"ok": True}

            elif method == "vault_list":
                keys = self._vault.list_keys(tag=params.get("tag"))
                return {"keys": keys}

            elif method == "vault_retrieve":
                return {
                    "error": "vault_retrieve is disabled. Secrets cannot be retrieved by sessions."
                }

            elif method == "vault_delete":
                key = params.get("key", "")
                if not key:
                    return {"error": "Key is required."}
                log.info("vault_delete key='%s' by session %s", key, session_id[:8])
                return {"deleted": self._vault.delete(key)}

            else:
                return {"error": f"Unknown vault method: {method}"}

        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("Vault bridge handler error")
            return {"error": f"Internal error: {e}"}
