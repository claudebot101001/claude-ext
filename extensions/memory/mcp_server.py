#!/usr/bin/env python3
"""Memory MCP server — three-layer identity + persistent knowledge store.

Spawned by Claude Code per session.  Inherits MCPServerBase for protocol
handling.  Regular memory ops use direct file I/O via MEMORY_DIR env var.
Personality ops use bridge RPC for encryption/decryption via main process.
"""

import os
import sys
from pathlib import Path

# Ensure the project root is importable (mcp_server.py lives in extensions/memory/)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402
from extensions.memory.store import MemoryStore  # noqa: E402

# Paths that the AI cannot write to (human-authored only)
_READONLY_PATHS = {"constitution.md"}


class MemoryMCPServer(MCPServerBase):
    name = "memory"
    tools = [
        # -- Layer 1-3 + Knowledge Store: direct file I/O tools --
        {
            "name": "memory_read",
            "description": "Read a memory file by relative path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the memory file (e.g. 'topics/python.md')",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "memory_write",
            "description": (
                "Overwrite a memory file. Use for creating or rewriting files. "
                "Parent directories are created automatically. "
                "Note: constitution.md is read-only and cannot be written."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the memory file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write",
                    },
                    "expires": {
                        "type": "string",
                        "description": "Optional ISO 8601 expiry timestamp (e.g. '2025-02-01T00:00:00Z'). File auto-deleted after this time.",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "memory_append",
            "description": (
                "Append content to a memory file with automatic UTC timestamp. "
                "Creates the file if it doesn't exist. "
                "Note: constitution.md is read-only and cannot be appended to."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the memory file (e.g. 'daily/2025-01-15.md')",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to append",
                    },
                    "expires": {
                        "type": "string",
                        "description": "Optional ISO 8601 expiry timestamp. Sets/updates the file's expiry.",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "memory_search",
            "description": (
                "Search all memory files by keyword or regex pattern (case-insensitive). "
                "Returns matching sections with relevance ranking and heading context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keyword or regex pattern)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_list",
            "description": (
                "List memory files sorted by modification time (newest first). "
                "Optionally filter by subdirectory."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": "Optional subdirectory to list (e.g. 'topics', 'users', 'events')",
                    },
                },
            },
        },
        # -- Layer 2: Personality tools (bridge RPC for encryption) --
        {
            "name": "personality_read",
            "description": "Read the AI's personality principles. Encrypted at rest.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "personality_write",
            "description": "Overwrite the AI's personality principles. Encrypted at rest.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "Full personality content. One principle per line. "
                            "Format: '- <principle> → [YYYY-MM-DD: description](events/YYYY-MM-DD-slug.md)'"
                        ),
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "personality_append",
            "description": "Append a new personality principle. Encrypted at rest.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "principle": {
                        "type": "string",
                        "description": (
                            "New principle with its formative event hyperlink. "
                            "Format: '- <principle> → [YYYY-MM-DD: description](events/YYYY-MM-DD-slug.md)'"
                        ),
                    },
                },
                "required": ["principle"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "memory_read": self._handle_read,
            "memory_write": self._handle_write,
            "memory_append": self._handle_append,
            "memory_search": self._handle_search,
            "memory_list": self._handle_list,
            "personality_read": self._handle_personality_read,
            "personality_write": self._handle_personality_write,
            "personality_append": self._handle_personality_append,
        }

    def _get_store(self) -> MemoryStore:
        if not hasattr(self, "_store"):
            memory_dir = os.environ.get("MEMORY_DIR", "")
            if not memory_dir:
                raise RuntimeError("MEMORY_DIR not set")
            self._store = MemoryStore(Path(memory_dir))
        return self._store

    @staticmethod
    def _check_readonly(path: str) -> str | None:
        """Return error string if path is read-only, else None."""
        normalized = path.strip().lower()
        if normalized in _READONLY_PATHS:
            return (
                f"Error: '{path}' is read-only (human-authored constitutional rules). "
                "Only the human operator can edit this file directly."
            )
        return None

    # -- direct file I/O handlers -------------------------------------------

    def _handle_read(self, args: dict) -> str:
        path = args.get("path")
        if not path:
            return "Error: 'path' is required."
        store = self._get_store()
        try:
            content = store.read(path)
        except ValueError as e:
            return f"Error: {e}"
        if content is None:
            return "File not found."
        return content

    def _handle_write(self, args: dict) -> str:
        path = args.get("path")
        content = args.get("content")
        expires = args.get("expires")
        if not path:
            return "Error: 'path' is required."
        if content is None:
            return "Error: 'content' is required."
        err = self._check_readonly(path)
        if err:
            return err
        store = self._get_store()
        try:
            nbytes = store.write(path, content, expires=expires)
        except ValueError as e:
            return f"Error: {e}"
        msg = f"Written {nbytes} bytes to {path}"
        if expires:
            msg += f" (expires: {expires})"
        return msg

    def _handle_append(self, args: dict) -> str:
        path = args.get("path")
        content = args.get("content")
        expires = args.get("expires")
        if not path:
            return "Error: 'path' is required."
        if content is None:
            return "Error: 'content' is required."
        err = self._check_readonly(path)
        if err:
            return err
        store = self._get_store()
        try:
            nbytes = store.append(path, content, expires=expires)
        except ValueError as e:
            return f"Error: {e}"
        msg = f"Appended {nbytes} bytes to {path}"
        if expires:
            msg += f" (expires: {expires})"
        return msg

    def _handle_search(self, args: dict) -> str:
        query = args.get("query")
        if not query:
            return "Error: 'query' is required."
        store = self._get_store()
        try:
            results = store.search(query)
        except ValueError as e:
            return f"Error: {e}"
        if not results:
            return "No matches found."
        lines = []
        for r in results:
            if "heading" in r:
                lines.append(f"{r['file']} [{r['heading']}]: {r['snippet']}")
            else:
                lines.append(f"{r['file']}:{r['line']}: {r['text']}")
        return "\n".join(lines)

    def _handle_list(self, args: dict) -> str:
        subdir = args.get("subdir", "")
        store = self._get_store()
        try:
            files = store.list_files(subdir)
        except ValueError as e:
            return f"Error: {e}"
        if not files:
            return "No files found."
        lines = []
        for f in files:
            lines.append(f"{f['path']}  ({f['size']} bytes, {f['modified']})")
        return "\n".join(lines)

    # -- personality handlers (bridge RPC for encryption) --------------------

    def _handle_personality_read(self, args: dict) -> str:
        try:
            result = self.bridge.call("memory_personality_read", {})
        except Exception as e:
            return f"Error: Failed to read personality: {e}"
        if "error" in result:
            return f"Error: {result['error']}"
        content = result.get("content")
        if content is None:
            return "No personality principles recorded yet."
        return content

    def _handle_personality_write(self, args: dict) -> str:
        content = args.get("content")
        if content is None:
            return "Error: 'content' is required."
        try:
            result = self.bridge.call("memory_personality_write", {"content": content})
        except Exception as e:
            return f"Error: Failed to write personality: {e}"
        if "error" in result:
            return f"Error: {result['error']}"
        return f"Personality principles written ({result.get('bytes', 0)} bytes encrypted)"

    def _handle_personality_append(self, args: dict) -> str:
        principle = args.get("principle")
        if not principle:
            return "Error: 'principle' is required."
        try:
            result = self.bridge.call("memory_personality_append", {"content": principle})
        except Exception as e:
            return f"Error: Failed to append personality: {e}"
        if "error" in result:
            return f"Error: {result['error']}"
        return f"Personality principle appended ({result.get('bytes', 0)} bytes encrypted)"


if __name__ == "__main__":
    MemoryMCPServer().run()
