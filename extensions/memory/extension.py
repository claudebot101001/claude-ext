"""Memory extension — persistent cross-session knowledge store.

Provides Markdown-on-disk memory that Claude agents maintain autonomously.
No bridge RPC needed: MCP server reads/writes files directly.
"""

import logging
import sys
from pathlib import Path

from core.extension import Extension
from extensions.memory.store import MemoryStore

log = logging.getLogger(__name__)

_SEED_MEMORY = """\
# Memory

This is your persistent memory file. It persists across sessions.

## User Preferences
<!-- Record user preferences here -->

## Active Projects
<!-- Track active projects and their status -->

## Key Decisions
<!-- Important decisions and their rationale -->

## Topic Files
<!-- Cross-references to detailed topic files in topics/ -->
"""

_SYSTEM_PROMPT = """\
You have a cross-session memory store (separate from Claude Code auto-memory). \
Manage it ONLY via MCP tools: memory_read, memory_write, memory_append, memory_search, memory_list. \
Do NOT use built-in Read/Write/Edit on these files.

SESSION START: call memory_read('MEMORY.md'). Read topic files as needed.

RECORDING: After significant tasks, memory_append('daily/YYYY-MM-DD.md', '- learned X about Y'). \
Record preferences, conventions, patterns, decisions.

FILES (relative paths, all via MCP):
- MEMORY.md: Hot index (<200 lines). Rewrite periodically to stay concise.
- topics/<name>.md: Deep knowledge per subject.
- daily/YYYY-MM-DD.md: Append-only log.

CURATION: When MEMORY.md exceeds ~150 lines, consolidate and move detail to topic files."""


class ExtensionImpl(Extension):
    name = "memory"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._store: MemoryStore | None = None

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # 1. Initialize MemoryStore
        memory_dir = Path(self.sm.base_dir) / "memory"
        self._store = MemoryStore(memory_dir)

        # 2. Register shared service
        self.engine.services["memory"] = self._store

        # 3. Register MCP server (inject MEMORY_DIR env var)
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        self.sm.register_mcp_server(
            "memory",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {"MEMORY_DIR": str(memory_dir)},
            },
            tools=[
                {"name": "memory_read", "description": "Read a memory file"},
                {"name": "memory_write", "description": "Overwrite/create a memory file"},
                {"name": "memory_append", "description": "Append content with UTC timestamp"},
                {"name": "memory_search", "description": "Regex search across all memory files"},
                {"name": "memory_list", "description": "List memory files by modification time"},
            ],
        )

        # 4. System prompt injection
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="memory")

        # 5. Seed MEMORY.md on first run
        if not (memory_dir / "MEMORY.md").exists():
            self._store.write("MEMORY.md", _SEED_MEMORY)
            log.info("Created seed MEMORY.md")

        log.info("Memory extension started. Store at %s", memory_dir)

    async def stop(self) -> None:
        self.engine.services.pop("memory", None)
        log.info("Memory extension stopped.")

    async def health_check(self) -> dict:
        if self._store is None:
            return {"status": "error", "detail": "MemoryStore not initialized"}
        files = self._store.list_files()
        return {"status": "ok", "files": len(files)}
