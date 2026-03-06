"""Memory extension — three-layer identity + knowledge graph + persistent store.

Layers:
1. Constitution: human-authored rules, AI read-only, injected into every session
2. Personality: AI self-developed principles, Fernet-encrypted, AI-managed via MCP
3. User Profile: per-user aspirations, injected per-session via customizer

Knowledge graph: MAGMA-style structured notes with tags, keywords, relations,
importance/decay scoring. Post-task reflection for graph evolution.

Knowledge store: general.md (identity + topic index, force-read at session start),
topics/<name>.md (deep knowledge), searchable via FTS5. Personality encryption uses Vault-managed
key via bridge RPC; all other memory ops use direct file I/O.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from core.extension import Extension
from core.session import SessionOverrides
from extensions.memory.migration import migrate, migrate_v3, needs_migration, needs_migration_v3
from extensions.memory.store import MemoryStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed content for first-run file creation
# ---------------------------------------------------------------------------


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

SESSION START: call personality_read() to load your personality principles.
ALSO: call memory_read('general.md') — it contains your identity, assets, vault keys, and topic index."""


class ExtensionImpl(Extension):
    name = "memory"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._store: MemoryStore | None = None
        self._personality_key: str | None = None
        self._graph = None
        self._reflector = None
        self._last_reflect: dict[str, float] = {}  # session_id -> timestamp
        self._injection_cache: dict[
            str, tuple[float, list[str]]
        ] = {}  # session_id -> (ts, snippets)

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # 1. Initialize MemoryStore
        memory_dir = Path(self.sm.base_dir) / "memory"
        self._store = MemoryStore(memory_dir)

        # 2. Run migrations if needed
        if needs_migration(memory_dir):
            migrate(memory_dir)
        if needs_migration_v3(memory_dir):
            migrate_v3(memory_dir)

        # 2b. Initialize KnowledgeGraph (own DB connection)
        from extensions.memory.graph import KnowledgeGraph

        self._graph = KnowledgeGraph(
            memory_dir,
            half_life_days=self.config.get("knowledge_injection", {}).get("half_life_days", 30),
        )
        self.engine.services["knowledge_graph"] = self._graph

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
                {
                    "name": "memory_write",
                    "description": "Overwrite/create a memory file (supports frontmatter)",
                },
                {"name": "memory_append", "description": "Append content with UTC timestamp"},
                {"name": "memory_search", "description": "Search with tag/importance filtering"},
                {"name": "memory_list", "description": "List memory files by modification time"},
                {
                    "name": "memory_meta",
                    "description": "Get/set note metadata (tags, keywords, importance)",
                },
                {"name": "memory_relate", "description": "Add/remove/list knowledge graph edges"},
                {"name": "memory_graph", "description": "Graph traversal, link suggestions, stats"},
                {
                    "name": "memory_import",
                    "description": "Batch import: content + meta + relations",
                },
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

        # 9. Session customizers — inject constitution + user profile + knowledge per-session
        self.sm.add_session_customizer(self._constitution_customizer)
        self.sm.add_session_customizer(self._user_profile_customizer)

        ki_config = self.config.get("knowledge_injection", {})
        if ki_config.get("enabled", True):
            self.sm.add_session_customizer(self._knowledge_injection_customizer)

        # 10. Reflection engine + delivery callback
        from extensions.memory.reflect import ReflectionEngine

        reflection_config = self.config.get("reflection", {})
        self._reflector = ReflectionEngine(self._graph, config=reflection_config)
        self.sm.add_delivery_callback(self._on_delivery)

        # 11. Seed files on first run
        self._seed_files(memory_dir)

        log.info(
            "Memory extension started. Store at %s (personality_key=%s)",
            memory_dir,
            "yes" if self._personality_key else "no",
        )

    async def stop(self) -> None:
        self._personality_key = None
        if self._graph:
            self._graph.close()
            self._graph = None
        self.engine.services.pop("memory", None)
        self.engine.services.pop("knowledge_graph", None)
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

    # -- knowledge injection customizer -------------------------------------

    def _knowledge_injection_customizer(self, session) -> SessionOverrides | None:
        """Auto-inject top-importance notes into session system prompt.

        Three-stage selection:
        1. If session.context has 'tags', match those first (high weight)
        2. If session.context has 'audit_target', FTS5 fuzzy search (medium weight)
        3. Fill remaining with top-N by effective_importance
        Cached per session for configured TTL.
        """
        if self._graph is None:
            return None

        ki_config = self.config.get("knowledge_injection", {})
        max_chars = ki_config.get("max_chars", 8000)
        max_notes = ki_config.get("max_notes", 10)
        cache_ttl = ki_config.get("cache_ttl_seconds", 60)

        session_id = getattr(session, "id", None) or ""

        # Prune stale cache entries (cap at 100)
        now = time.time()
        if len(self._injection_cache) > 100:
            stale = [k for k, (ts, _) in self._injection_cache.items() if now - ts > cache_ttl]
            for k in stale:
                del self._injection_cache[k]

        # Check cache
        if session_id in self._injection_cache:
            cached_ts, cached_snippets = self._injection_cache[session_id]
            if now - cached_ts < cache_ttl and cached_snippets:
                return SessionOverrides(
                    extra_system_prompt=[
                        "## KNOWLEDGE CONTEXT (auto-injected)\n" + "\n---\n".join(cached_snippets)
                    ]
                )

        # Stage 1: Context tag matching (high weight)
        context = getattr(session, "context", {}) or {}
        context_tags = context.get("tags", [])

        candidates = []
        seen = set()

        if context_tags:
            tag_results = self._graph.top_notes(limit=max_notes, tags=context_tags)
            for r in tag_results:
                candidates.append(r)
                seen.add(r["path"])

        # Stage 2: FTS5 fuzzy search for audit_target (medium weight)
        audit_target = context.get("audit_target", "")
        if audit_target and self._store is not None and len(candidates) < max_notes:
            fts_results = self._store.search(str(audit_target))
            for r in fts_results[: max_notes - len(candidates)]:
                path = r.get("file", "")
                if path and path not in seen:
                    # Get effective importance from graph
                    meta = self._graph.get_meta(path)
                    eff_imp = meta.get("effective_importance", 0.5) if meta else 0.5
                    candidates.append({"path": path, "effective_importance": eff_imp})
                    seen.add(path)

        # Stage 3: Fill remaining with top by importance
        remaining = max_notes - len(candidates)
        if remaining > 0:
            top_results = self._graph.top_notes(limit=remaining + len(seen))
            for r in top_results:
                if r["path"] not in seen:
                    candidates.append(r)
                    if len(candidates) >= max_notes:
                        break

        if not candidates:
            return None

        # Build snippets (first heading + first paragraph, max 500 chars each)
        snippets = []
        total_chars = 0
        for c in candidates:
            if total_chars >= max_chars:
                break
            if self._store is None:
                break
            content = self._store.read(c["path"])
            if not content:
                continue
            from extensions.memory.frontmatter import strip_frontmatter

            body = strip_frontmatter(content)
            snippet = self._truncate_note(body, 500)
            snippet_with_meta = f"**{c['path']}** (imp={c['effective_importance']})\n{snippet}"
            if total_chars + len(snippet_with_meta) > max_chars:
                break
            snippets.append(snippet_with_meta)
            total_chars += len(snippet_with_meta)

        if not snippets:
            return None

        # Cache
        self._injection_cache[session_id] = (now, snippets)

        return SessionOverrides(
            extra_system_prompt=[
                "## KNOWLEDGE CONTEXT (auto-injected)\n" + "\n---\n".join(snippets)
            ]
        )

    @staticmethod
    def _truncate_note(body: str, max_chars: int = 500) -> str:
        """Truncate to first heading + first paragraph."""
        lines = body.strip().splitlines()
        result = []
        total = 0
        for line in lines:
            if total + len(line) > max_chars:
                result.append("...")
                break
            result.append(line)
            total += len(line) + 1
            # Stop after first blank line following content
            if total > 50 and line.strip() == "":
                break
        return "\n".join(result)

    # -- delivery callback (reflection) -------------------------------------

    async def _on_delivery(self, session_id: str, text: str, metadata: dict) -> None:
        """Post-task reflection via delivery callback."""
        if not metadata.get("is_final"):
            return
        # Skip heartbeat/subagent sessions
        if metadata.get("is_heartbeat") or metadata.get("is_subagent"):
            return
        if self._reflector is None:
            return

        # Rate limit: 1 per session per 5 min
        now = time.time()
        last = self._last_reflect.get(session_id, 0)
        if now - last < 300:
            return
        self._last_reflect[session_id] = now

        # Prune stale entries (older than 1 hour)
        if len(self._last_reflect) > 100:
            stale = [k for k, ts in self._last_reflect.items() if now - ts > 3600]
            for k in stale:
                del self._last_reflect[k]

        # Run reflection in background to not block delivery
        asyncio.create_task(self._safe_reflect(session_id, text, metadata))

    async def _safe_reflect(self, session_id: str, text: str, metadata: dict) -> None:
        """Wrap reflection in try/except to prevent unhandled task exceptions."""
        try:
            # L1: deterministic (always runs)
            actions = self._reflector.reflect(session_id, text, metadata)
            if actions:
                applied = self._reflector.apply(actions, store=self._store)
                log.info(
                    "L1 reflection for session %s: %d actions proposed, %d applied",
                    session_id[:8],
                    len(actions),
                    applied,
                )

            # L2: LLM-assisted (conditional)
            if self._reflector.should_trigger_l2(text):
                await self._run_l2_reflection(session_id, text)

        except Exception:
            log.exception("Reflection failed for session %s", session_id[:8])

    async def _run_l2_reflection(self, session_id: str, text: str) -> None:
        """Run L2 LLM reflection using Sonnet."""
        if self._graph is None or self._reflector is None:
            return

        # Gather existing notes context
        existing_notes = []
        top = self._graph.top_notes(limit=30, min_importance=0.1)
        for n in top:
            meta = self._graph.get_meta(n["path"])
            if meta:
                existing_notes.append(meta)

        prompt = self._reflector.build_l2_prompt(text, existing_notes)

        l2_model = self.config.get("reflection", {}).get("llm_model", "claude-sonnet-4-6")
        log.info("L2 reflection: calling %s for session %s", l2_model, session_id[:8])

        try:
            response = await self.engine.ask(
                prompt,
                model=l2_model,
                max_turns=1,
                timeout=60,
            )
        except Exception:
            log.exception("L2 reflection: engine.ask failed")
            return

        if response.startswith("[Error]"):
            log.warning("L2 reflection: %s", response)
            return

        actions = self._reflector.parse_l2_response(response)
        if actions:
            applied = self._reflector.apply(actions, store=self._store)
            log.info(
                "L2 reflection for session %s: %d actions proposed, %d applied",
                session_id[:8],
                len(actions),
                applied,
            )

    # -- seed files ---------------------------------------------------------

    def _seed_files(self, memory_dir: Path) -> None:
        """Create seed files on first run."""
        # constitution.md is seeded by migration.py
        # users/ and events/ directories are created by migration.py
        pass
