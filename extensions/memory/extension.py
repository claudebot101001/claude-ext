"""Memory extension — three-layer identity system + persistent knowledge store.

Layers:
1. Constitution: human-authored rules, AI read-only, injected into every session
2. Personality: AI self-developed principles, Fernet-encrypted, AI-managed via MCP
3. User Profile: per-user aspirations, injected per-session via customizer

Knowledge store: TOPICS_INDEX.md (topic catalog), topics/<name>.md (deep
knowledge), searchable via FTS5. Personality encryption uses Vault-managed
key via bridge RPC; all other memory ops use direct file I/O.
"""

import logging
import os
import sys
from pathlib import Path

from core.extension import Extension
from core.session import SessionOverrides
from extensions.memory.migration import migrate, needs_migration
from extensions.memory.store import MemoryStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed content for first-run file creation
# ---------------------------------------------------------------------------

_SEED_TOPICS_INDEX = """\
# Topics Index

Detailed descriptions of topic files for search matching.
Update this file whenever you create or significantly modify a topic.

## Entries
<!-- Add entries as topic files are created -->
"""

# ---------------------------------------------------------------------------
# System prompt — three-layer identity + knowledge store guidance
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You have a cross-session memory store with a three-layer identity system. \
Manage it ONLY via MCP tools — do NOT use built-in Read/Write/Edit on these files.

## Layer 1: Constitution (read-only)
constitution.md contains your foundational rules, authored by the human operator. \
You CANNOT modify it. It is injected into your system prompt automatically.

## Layer 2: Personality (encrypted, AI-managed)
Your self-developed behavioral principles. Use personality_read, personality_write, personality_append. \
Each principle MUST have a hyperlinked formative event: a specific, dated, verifiable experience \
that explains WHY you adopted this principle. Store event details in events/<date>-<slug>.md. \
Format: "- <principle> → [YYYY-MM-DD: brief description](events/YYYY-MM-DD-slug.md)"

## Layer 3: User Profile
Per-user preferences at users/<user_id>/profile.md. Users write their aspirations and demands \
(not definitions). Injected into your system prompt per-session.

## Knowledge Store
Persistent knowledge is stored in topic files (topics/<name>.md). \
Use memory_search to find relevant knowledge when needed. \
Use memory_read/memory_write/memory_append to manage files directly.

SESSION START: call personality_read() to load your personality principles."""


class ExtensionImpl(Extension):
    name = "memory"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._store: MemoryStore | None = None
        self._personality_key: str | None = None

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # 1. Initialize MemoryStore
        memory_dir = Path(self.sm.base_dir) / "memory"
        self._store = MemoryStore(memory_dir)

        # 2. Run v1→v2 migration if needed
        if needs_migration(memory_dir):
            migrate(memory_dir)

        # 3. Register shared service (used by heartbeat etc.)
        self.engine.services["memory"] = self._store

        # 4. Initialize personality encryption key (requires vault)
        await self._ensure_personality_key()

        # 5. Register bridge handler for personality encrypt/decrypt
        self.engine.bridge.add_handler(self._handle_bridge)

        # 6. Register MCP server with all 8 tools
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
                {"name": "memory_search", "description": "Search across all memory files"},
                {"name": "memory_list", "description": "List memory files by modification time"},
                {
                    "name": "personality_read",
                    "description": "Read AI personality principles (decrypted)",
                },
                {
                    "name": "personality_write",
                    "description": "Overwrite AI personality principles (encrypted)",
                },
                {
                    "name": "personality_append",
                    "description": "Append a new AI personality principle (encrypted)",
                },
            ],
        )

        # 7. Disable CC built-in auto-memory (claude-ext manages its own)
        os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

        # 8. System prompt (tagged so it can be excluded per-session)
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="memory")

        # 8. Session customizers — inject constitution + user profile per-session
        self.sm.add_session_customizer(self._constitution_customizer)
        self.sm.add_session_customizer(self._user_profile_customizer)

        # 9. Seed files on first run
        self._seed_files(memory_dir)

        log.info(
            "Memory extension started. Store at %s (personality_key=%s)",
            memory_dir,
            "yes" if self._personality_key else "no",
        )

    async def stop(self) -> None:
        self._personality_key = None
        self.engine.services.pop("memory", None)
        log.info("Memory extension stopped.")

    async def health_check(self) -> dict:
        if self._store is None:
            return {"status": "error", "detail": "MemoryStore not initialized"}
        files = self._store.list_files()
        return {
            "status": "ok",
            "files": len(files),
            "personality_encrypted": self._personality_key is not None,
        }

    # -- personality encryption key -----------------------------------------

    async def _ensure_personality_key(self) -> None:
        """Retrieve or generate personality encryption key from Vault."""
        vault = self.engine.services.get("vault")
        if vault is None:
            log.warning("Vault not enabled; personality encryption disabled (plaintext fallback)")
            return

        key_name = "memory/personality/encryption_key"
        try:
            key = vault.get(key_name)
        except Exception:
            log.exception("Failed to retrieve personality key from vault")
            return

        if key is None:
            from extensions.memory.crypto import generate_key

            key = generate_key()
            try:
                vault.put(key_name, key, ["memory", "personality", "auto-generated"])
                log.info("Generated new personality encryption key in vault")
            except Exception:
                log.exception("Failed to store personality key in vault")
                return

        self._personality_key = key

    # -- bridge handler (personality encrypt/decrypt) -----------------------

    async def _handle_bridge(self, method: str, params: dict) -> dict | None:
        """Handle memory bridge RPCs from MCP server processes."""
        if not method.startswith("memory_personality_"):
            return None  # not ours

        handlers = {
            "memory_personality_read": self._bridge_personality_read,
            "memory_personality_write": self._bridge_personality_write,
            "memory_personality_append": self._bridge_personality_append,
        }
        handler = handlers.get(method)
        if handler is None:
            return {"error": f"Unknown memory method: {method}"}

        try:
            return await handler(params)
        except Exception:
            log.exception("Memory bridge handler error for %s", method)
            return {"error": f"Internal error handling {method}"}

    async def _bridge_personality_read(self, params: dict) -> dict:
        if self._personality_key is None:
            return {"error": "Personality encryption not available (vault not enabled)"}
        assert self._store is not None
        raw = self._store.read_personality_raw()
        if raw is None:
            return {"content": None}
        from extensions.memory.crypto import decrypt_personality

        plaintext = decrypt_personality(raw, self._personality_key)
        return {"content": plaintext}

    async def _bridge_personality_write(self, params: dict) -> dict:
        content = params.get("content", "")
        if self._personality_key is None:
            return {"error": "Personality encryption not available (vault not enabled)"}
        assert self._store is not None
        from extensions.memory.crypto import encrypt_personality

        encrypted = encrypt_personality(content, self._personality_key)
        nbytes = self._store.write_personality_raw(encrypted)
        return {"ok": True, "bytes": nbytes}

    async def _bridge_personality_append(self, params: dict) -> dict:
        content = params.get("content", "")
        if self._personality_key is None:
            return {"error": "Personality encryption not available (vault not enabled)"}
        assert self._store is not None
        from extensions.memory.crypto import decrypt_personality, encrypt_personality

        raw = self._store.read_personality_raw()
        if raw:
            existing = decrypt_personality(raw, self._personality_key)
        else:
            existing = ""
        combined = existing.rstrip("\n") + "\n" + content + "\n"
        encrypted = encrypt_personality(combined, self._personality_key)
        nbytes = self._store.write_personality_raw(encrypted)
        return {"ok": True, "bytes": nbytes}

    # -- session customizers ------------------------------------------------

    def _constitution_customizer(self, session) -> SessionOverrides | None:
        """Inject constitution.md into every session's system prompt."""
        if self._store is None:
            return None
        content = self._store.read("constitution.md")
        if not content:
            return None
        # Skip injection if file contains only headings, comments, and blanks
        # (i.e. the seed template with no real rules added yet)
        has_real_content = any(
            line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("<!--")
            for line in content.splitlines()
        )
        if not has_real_content:
            return None
        return SessionOverrides(
            extra_system_prompt=[f"## CONSTITUTIONAL RULES (immutable, human-authored)\n{content}"]
        )

    def _user_profile_customizer(self, session) -> SessionOverrides | None:
        """Inject user profile into session system prompt."""
        if self._store is None:
            return None
        user_id = getattr(session, "user_id", None)
        if not user_id:
            return None
        profile_path = f"users/{user_id}/profile.md"
        content = self._store.read(profile_path)
        if not content:
            return None
        return SessionOverrides(
            extra_system_prompt=[f"## USER PROFILE (user_id={user_id})\n{content}"]
        )

    # -- seed files ---------------------------------------------------------

    def _seed_files(self, memory_dir: Path) -> None:
        """Create seed files on first run."""
        assert self._store is not None
        if not (memory_dir / "TOPICS_INDEX.md").exists():
            self._store.write("TOPICS_INDEX.md", _SEED_TOPICS_INDEX)
            log.info("Created seed TOPICS_INDEX.md")
        # constitution.md is seeded by migration.py
        # users/ and events/ directories are created by migration.py
