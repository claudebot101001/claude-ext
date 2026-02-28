"""Vault extension — encrypted credential storage for Claude Code sessions.

Provides:
1. ``engine.services["vault"]`` — VaultStore instance for programmatic access
   by other extensions (e.g. wallet can decrypt private keys without LLM).
2. MCP tools (vault_store/list/retrieve/delete) for Claude to manage secrets.
3. System prompt guidance to prevent echoing secrets to users.

Passphrase is read from ``CLAUDE_EXT_VAULT_PASSPHRASE`` environment variable.
"""

import logging
import os
import sys
from pathlib import Path

from core.extension import Extension
from extensions.vault.store import VaultStore

log = logging.getLogger(__name__)


class ExtensionImpl(Extension):
    name = "vault"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._vault: VaultStore | None = None

    @property
    def sm(self):
        return self.engine.session_manager

    async def start(self) -> None:
        # Read passphrase from environment
        passphrase = os.environ.get("CLAUDE_EXT_VAULT_PASSPHRASE", "")
        if not passphrase:
            raise RuntimeError(
                "CLAUDE_EXT_VAULT_PASSPHRASE environment variable is required. "
                "Set it before starting claude-ext."
            )

        # Initialize vault store
        state_dir = self.sm.base_dir if self.sm else Path("~/.claude-ext").expanduser()
        vault_dir = state_dir / "vault"
        self._vault = VaultStore(vault_dir, passphrase)

        # Register as shared service for other extensions
        self.engine.services["vault"] = self._vault

        # Register MCP server for Claude session access
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server("vault", {
            "command": sys.executable,
            "args": [mcp_script],
            "env": {},  # No passphrase here — MCP uses bridge RPC
        })

        # Register bridge handler for MCP → main process calls
        self.engine.bridge.add_handler(self._bridge_handler)

        # System prompt: instruct Claude to never leak secrets
        self.sm.add_system_prompt(
            "You have access to an encrypted credential vault via MCP tools "
            "(vault_store, vault_list, vault_retrieve, vault_delete). "
            "CRITICAL: Never echo or display secret values to the user. "
            "When you retrieve a secret, use it directly in subsequent tool "
            "calls (e.g. as an API key in a curl command, as a password in "
            "a config file). If the user asks to see a secret value, remind "
            "them to check the vault directly for security reasons."
        )

        log.info(
            "Vault extension started. %d secret(s) stored.",
            len(self._vault.list_keys()),
        )

    async def stop(self) -> None:
        self.engine.services.pop("vault", None)
        log.info("Vault extension stopped.")

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        """Handle vault bridge RPCs from MCP server processes."""
        if not method.startswith("vault_"):
            return None  # not ours

        if self._vault is None:
            return {"error": "Vault not initialized"}

        try:
            if method == "vault_store":
                self._vault.put(
                    key=params["key"],
                    value=params["value"],
                    tags=params.get("tags"),
                )
                return {"ok": True}

            elif method == "vault_list":
                keys = self._vault.list_keys(tag=params.get("tag"))
                return {"keys": keys}

            elif method == "vault_retrieve":
                value = self._vault.get(params["key"])
                if value is None:
                    return {"value": None}
                return {"value": value}

            elif method == "vault_delete":
                deleted = self._vault.delete(params["key"])
                return {"deleted": deleted}

            else:
                return {"error": f"Unknown vault method: {method}"}

        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("Vault bridge handler error")
            return {"error": f"Internal error: {e}"}
