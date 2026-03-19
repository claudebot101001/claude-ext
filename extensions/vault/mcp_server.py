#!/usr/bin/env python3
"""Vault MCP server — credential management via Claude tool calls.

Spawned by Claude Code per session.  Uses bridge RPC to call VaultStore
in the main process (avoids each MCP process needing the passphrase).
"""

import os
import sys
from pathlib import Path

# Ensure the project root is importable (mcp_server.py lives in extensions/vault/)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class VaultMCPServer(MCPServerBase):
    name = "vault"
    gateway_description = (
        "Encrypted credential store (store/list/delete). action='help' for details."
    )
    tools = [
        {
            "name": "vault_store",
            "description": (
                "Store a secret credential in the encrypted vault. "
                "Use this for API keys, passwords, tokens, etc. "
                "If the key already exists, it will be overwritten."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Namespaced key for the secret. Use 'category/service/name' format, e.g. 'email/smtp/password', 'wallet/eth/privkey', 'api/github/token'",
                    },
                    "value": {
                        "type": "string",
                        "description": "The secret value to store",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorization (e.g. ['email', 'production'])",
                    },
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "vault_list",
            "description": (
                "List all stored credential keys and their tags. "
                "Does NOT return secret values — only key names and tags."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Optional: filter by tag",
                    },
                },
            },
        },
        {
            "name": "vault_delete",
            "description": "Delete a secret from the vault.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "The key of the secret to delete",
                    },
                },
                "required": ["key"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "vault_store": self._handle_store,
            "vault_list": self._handle_list,
            "vault_delete": self._handle_delete,
        }

    def _bridge_call(self, method: str, params: dict) -> dict:
        """Call the main process via bridge RPC.

        Automatically injects session_id so the handler can identify
        the calling session (needed for future access control).
        """
        if not self.bridge:
            raise RuntimeError("Bridge not available. Vault cannot function without it.")
        params = {**params, "session_id": self.session_id}
        timeout = float(os.environ.get("VAULT_BRIDGE_TIMEOUT", "30"))
        return self.bridge.call(method, params, timeout=timeout)

    # -- handlers -----------------------------------------------------------

    def _handle_store(self, args: dict) -> str:
        key = args.get("key", "")
        value = args.get("value", "")
        tags = args.get("tags") or []

        if not key:
            return "Error: 'key' is required."
        if not value:
            return "Error: 'value' is required."

        try:
            result = self._bridge_call(
                "vault_store",
                {
                    "key": key,
                    "value": value,
                    "tags": tags,
                },
            )
        except (TimeoutError, ConnectionError, RuntimeError) as e:
            return f"Error: {e}"

        if result.get("error"):
            return f"Error: {result['error']}"
        return f"Stored secret '{key}' in vault."

    def _handle_list(self, args: dict) -> str:
        tag = args.get("tag")

        try:
            result = self._bridge_call("vault_list", {"tag": tag})
        except (TimeoutError, ConnectionError, RuntimeError) as e:
            return f"Error: {e}"

        if result.get("error"):
            return f"Error: {result['error']}"

        keys = result.get("keys", [])
        if not keys:
            return "Vault is empty." if not tag else f"No secrets with tag '{tag}'."

        lines = []
        for entry in keys:
            tags_str = f" [{', '.join(entry['tags'])}]" if entry.get("tags") else ""
            lines.append(f"  - {entry['key']}{tags_str}")
        return f"{len(keys)} secret(s):\n" + "\n".join(lines)

    def _handle_delete(self, args: dict) -> str:
        key = args.get("key", "")
        if not key:
            return "Error: 'key' is required."

        try:
            result = self._bridge_call("vault_delete", {"key": key})
        except (TimeoutError, ConnectionError, RuntimeError) as e:
            return f"Error: {e}"

        if result.get("error"):
            return f"Error: {result['error']}"

        if result.get("deleted"):
            return f"Deleted secret '{key}' from vault."
        return f"Secret '{key}' not found in vault."


if __name__ == "__main__":
    VaultMCPServer().run()
