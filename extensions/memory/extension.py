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
IMPORTANT: This is a SEPARATE memory system from Claude Code's built-in auto-memory \
(~/.claude/projects/). Do NOT use the built-in Read/Write tools to manage memory files. \
Use ONLY the MCP tools below. They read and write to a dedicated shared store that \
persists across ALL sessions regardless of working directory.

Available MCP tools: memory_read, memory_write, memory_append, memory_search, memory_list.

SESSION START PROTOCOL:
1. At the start of every session, call memory_read('MEMORY.md') via MCP tool
2. If relevant topics are mentioned, read the specific topic file via memory_read

RECORDING LEARNINGS:
After completing significant tasks, append key learnings to today's daily log:
  memory_append('daily/YYYY-MM-DD.md', '- learned X about Y')
Record: user preferences, project conventions, recurring patterns, decisions made, \
things that worked or failed.

FILE ORGANIZATION (all paths are relative, managed by MCP tools):
- MEMORY.md: Hot index. Keep under 200 lines. Contains the most important current \
knowledge: active projects, user preferences, key decisions, cross-references to \
topic files. Rewrite periodically to stay concise.
- topics/<name>.md: Deep knowledge on specific subjects. Create when a topic grows \
beyond a few lines in MEMORY.md.
- daily/YYYY-MM-DD.md: Append-only daily log. Raw learnings and observations.

CURATION (critical):
When MEMORY.md grows beyond ~150 lines, rewrite it: consolidate, remove stale \
entries, move detailed content to topic files. Prefer rewriting over appending. \
The goal is a concise, high-signal index, not a changelog."""


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
        self.sm.register_mcp_server("memory", {
            "command": sys.executable,
            "args": [mcp_script],
            "env": {"MEMORY_DIR": str(memory_dir)},
        })

        # 4. System prompt injection
        self.sm.add_system_prompt(_SYSTEM_PROMPT)

        # 5. Seed MEMORY.md on first run
        if not (memory_dir / "MEMORY.md").exists():
            self._store.write("MEMORY.md", _SEED_MEMORY)
            log.info("Created seed MEMORY.md")

        log.info("Memory extension started. Store at %s", memory_dir)

    async def stop(self) -> None:
        self.engine.services.pop("memory", None)
        log.info("Memory extension stopped.")
