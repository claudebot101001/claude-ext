#!/usr/bin/env python3
"""Memory MCP server — persistent knowledge store via Claude tool calls.

Spawned by Claude Code per session.  Inherits MCPServerBase for protocol
handling; uses direct file I/O via MEMORY_DIR environment variable.
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


class MemoryMCPServer(MCPServerBase):
    name = "memory"
    tools = [
        {
            "name": "memory_read",
            "description": (
                "Read a memory file. Use 'MEMORY.md' for the main index, "
                "'topics/<name>.md' for topic files, 'daily/YYYY-MM-DD.md' for daily logs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the memory file (e.g. 'MEMORY.md', 'topics/python.md')",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "memory_write",
            "description": (
                "Overwrite a memory file. Use for creating or rewriting files. "
                "Parent directories are created automatically."
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
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "memory_append",
            "description": (
                "Append content to a memory file with automatic UTC timestamp. "
                "Ideal for daily logs. Creates the file if it doesn't exist."
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
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "memory_search",
            "description": (
                "Search all memory files by keyword or regex pattern (case-insensitive). "
                "Returns matching lines with file paths and line numbers."
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
                        "description": "Optional subdirectory to list (e.g. 'topics', 'daily')",
                    },
                },
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
        }

    def _get_store(self) -> MemoryStore:
        if not hasattr(self, "_store"):
            memory_dir = os.environ.get("MEMORY_DIR", "")
            if not memory_dir:
                raise RuntimeError("MEMORY_DIR not set")
            self._store = MemoryStore(Path(memory_dir))
        return self._store

    # -- handlers -----------------------------------------------------------

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
        if not path:
            return "Error: 'path' is required."
        if content is None:
            return "Error: 'content' is required."
        store = self._get_store()
        try:
            nbytes = store.write(path, content)
        except ValueError as e:
            return f"Error: {e}"
        return f"Written {nbytes} bytes to {path}"

    def _handle_append(self, args: dict) -> str:
        path = args.get("path")
        content = args.get("content")
        if not path:
            return "Error: 'path' is required."
        if content is None:
            return "Error: 'content' is required."
        store = self._get_store()
        try:
            nbytes = store.append(path, content)
        except ValueError as e:
            return f"Error: {e}"
        return f"Appended {nbytes} bytes to {path}"

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


if __name__ == "__main__":
    MemoryMCPServer().run()
