"""Memory extension — three-layer identity + domain-scoped knowledge stores.

Layers:
1. Constitution: human-authored rules, AI read-only, injected into every session
2. Personality: AI self-developed principles, Fernet-encrypted, AI-managed via MCP
3. User Profile: per-user aspirations, injected per-session via customizer

Knowledge store: general.md (identity + topic index, force-read at session start),
topics/<name>.md (operational knowledge), searchable via FTS5.

Domain knowledge (MAGMA): isolated per-domain stores under domains/<name>/.
Each domain has its own MemoryStore + KnowledgeGraph + FTS5 index.
Sessions declare accessible domains via context.domains list.
Personality encryption uses Vault-managed key via bridge RPC.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from core.extension import Extension
from core.session import SessionOverrides
from extensions.memory.migration import (
    migrate,
    migrate_v3,
    migrate_v4,
    needs_migration,
    needs_migration_v3,
    needs_migration_v4,
)

log = logging.getLogger(__name__)

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

## Domain Knowledge
Some sessions have access to isolated domain knowledge stores (e.g. vuln patterns). \
When a domain is available, memory tools accept an optional `domain` parameter to \
operate on domain-scoped files. Without `domain`, tools operate on core memory only.

SESSION START: call personality_read() to load your personality principles.
ALSO: call memory_read('general.md') — it contains your identity, assets, vault keys, and topic index."""


class ExtensionImpl(Extension):
    name = "memory"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._store = None
        self._personality_key: str | None = None
        self._graph = None
        self._domain_manager = None
        self._last_reflect: dict[str, float] = {}  # session_id -> timestamp
        self._injection_cache: dict[
            str, tuple[float, list[str]]
        ] = {}  # session_id -> (ts, snippets)
        self._base_memory_mcp_config: dict = {}  # saved for domain env override

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        from extensions.memory.store import MemoryStore

        # 1. Initialize core MemoryStore (exclude domain dirs from core index)
        memory_dir = Path(self.sm.base_dir) / "memory"
        self._store = MemoryStore(memory_dir, exclude_dirs={"domains"})

        # 2. Run migrations if needed
        if needs_migration(memory_dir):
            migrate(memory_dir)
        if needs_migration_v3(memory_dir):
            migrate_v3(memory_dir)

        # 2b. Initialize core KnowledgeGraph
        from extensions.memory.graph import KnowledgeGraph

        self._graph = KnowledgeGraph(
            memory_dir,
            half_life_days=30,
        )
        self.engine.services["knowledge_graph"] = self._graph

        # 3. Register shared service
        self.engine.services["memory"] = self._store

        # 4. Initialize personality encryption key (requires vault)
        await self._ensure_personality_key()

        # 5. Register bridge handler for personality encrypt/decrypt
        self.engine.bridge.add_handler(self._handle_bridge)

        # 6. Register MCP server with all tools
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        self._base_memory_mcp_config = {
            "command": sys.executable,
            "args": [mcp_script],
            "env": {"MEMORY_DIR": str(memory_dir)},
        }
        self.sm.register_mcp_server(
            "memory",
            dict(self._base_memory_mcp_config),
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

        # 7. Disable CC built-in auto-memory
        os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

        # 8. System prompt
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="memory")

        # 9. Session customizers — inject constitution + user profile per-session
        self.sm.add_session_customizer(self._constitution_customizer)
        self.sm.add_session_customizer(self._user_profile_customizer)

        # 10. Domain knowledge (MAGMA infrastructure)
        magma_config = self.config.get("magma", {})
        domain_configs = magma_config.get("domains", {})

        if domain_configs:
            from extensions.memory.domains import DomainManager

            # Run v4 migration before initializing domains
            if needs_migration_v4(memory_dir):
                migrate_v4(memory_dir, domain_configs)

            self._domain_manager = DomainManager(memory_dir, domain_configs)
            self.engine.services["domain_manager"] = self._domain_manager

            # Domain scoping customizer (injects MEMORY_DOMAINS env per-session)
            self.sm.add_session_customizer(self._domain_scoping_customizer)

            # Knowledge injection customizer (per-domain, for sessions with domains)
            has_injection = any(
                d.get("knowledge_injection", {}).get("enabled", False)
                for d in domain_configs.values()
            )
            if has_injection:
                self.sm.add_session_customizer(self._knowledge_injection_customizer)

            # Reflection engine (per-domain)
            has_reflection = any(
                d.get("reflection", {}).get("llm_enabled", False) for d in domain_configs.values()
            )
            if has_reflection:
                self.sm.add_delivery_callback(self._on_delivery)

        # 11. Seed files on first run
        self._seed_files(memory_dir)

        log.info(
            "Memory extension started. Store at %s (personality_key=%s, domains=%s)",
            memory_dir,
            "yes" if self._personality_key else "no",
            self._domain_manager.list_domains() if self._domain_manager else "none",
        )

    async def stop(self) -> None:
        self._personality_key = None
        if self._graph:
            self._graph.close()
            self._graph = None
        if self._domain_manager:
            self._domain_manager.close_all()
            self._domain_manager = None
        self.engine.services.pop("memory", None)
        self.engine.services.pop("knowledge_graph", None)
        self.engine.services.pop("domain_manager", None)
        log.info("Memory extension stopped.")

    async def health_check(self) -> dict:
        if self._store is None:
            return {"status": "error", "detail": "MemoryStore not initialized"}
        files = self._store.list_files()
        domains = self._domain_manager.list_domains() if self._domain_manager else []
        return {
            "status": "ok",
            "files": len(files),
            "personality_encrypted": self._personality_key is not None,
            "domains": domains,
        }

    # -- personality encryption key -----------------------------------------

    async def _ensure_personality_key(self) -> None:
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
        if not method.startswith("memory_personality_"):
            return None

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
        if self._store is None:
            return None
        content = self._store.read("constitution.md")
        if not content:
            return None
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

    def _domain_scoping_customizer(self, session) -> SessionOverrides | None:
        """Inject MEMORY_DOMAINS env var into memory MCP server for domain sessions."""
        context = getattr(session, "context", {}) or {}
        domains = context.get("domains", [])
        if not domains or not self._domain_manager:
            return None
        valid = [d for d in domains if d in self._domain_manager.list_domains()]
        if not valid:
            return None
        # Override memory MCP server env to include domains
        mcp_config = dict(self._base_memory_mcp_config)
        env = dict(mcp_config.get("env", {}))
        env["MEMORY_DOMAINS"] = ",".join(valid)
        mcp_config["env"] = env

        # Build domain description for system prompt
        domain_descs = []
        for d in valid:
            cfg = self._domain_manager.get_config(d)
            desc = cfg.get("description", d)
            domain_descs.append(f"- **{d}**: {desc}")
        domain_prompt = (
            "## Available Knowledge Domains\n"
            "This session has access to domain-specific knowledge stores. "
            "Use `domain` parameter with memory tools to access them.\n" + "\n".join(domain_descs)
        )

        return SessionOverrides(
            extra_mcp_servers={"memory": mcp_config},
            extra_system_prompt=[domain_prompt],
        )

    # -- knowledge injection customizer (domain-scoped) ---------------------

    def _knowledge_injection_customizer(self, session) -> SessionOverrides | None:
        """Inject knowledge notes from domain stores into domain sessions."""
        if not self._domain_manager:
            return None

        context = getattr(session, "context", {}) or {}
        domains = context.get("domains", [])
        if not domains:
            return None

        session_id = getattr(session, "id", None) or ""

        # Cache check
        now = time.time()
        if len(self._injection_cache) > 100:
            stale = [k for k, (ts, _) in self._injection_cache.items() if now - ts > 60]
            for k in stale:
                del self._injection_cache[k]
        if session_id in self._injection_cache:
            cached_ts, cached_snippets = self._injection_cache[session_id]
            if now - cached_ts < 60 and cached_snippets:
                return SessionOverrides(
                    extra_system_prompt=[
                        "## KNOWLEDGE CONTEXT (auto-injected)\n" + "\n---\n".join(cached_snippets)
                    ]
                )

        snippets = []
        total_chars = 0

        for domain_name in domains:
            domain_graph = self._domain_manager.get_graph(domain_name)
            domain_store = self._domain_manager.get_store(domain_name)
            domain_config = self._domain_manager.get_config(domain_name)
            if not domain_graph or not domain_store:
                continue

            ki_config = domain_config.get("knowledge_injection", {})
            if not ki_config.get("enabled", False):
                continue

            max_chars = ki_config.get("max_chars", 8000)
            max_notes = ki_config.get("max_notes", 10)

            # Context tag matching
            context_tags = context.get("tags", [])
            candidates = []
            seen = set()

            if context_tags:
                tag_results = domain_graph.top_notes(limit=max_notes, tags=context_tags)
                for r in tag_results:
                    candidates.append(r)
                    seen.add(r["path"])

            # FTS5 fuzzy search for audit_target
            audit_target = context.get("audit_target", "")
            if audit_target and len(candidates) < max_notes:
                fts_results = domain_store.search(str(audit_target))
                for r in fts_results[: max_notes - len(candidates)]:
                    path = r.get("file", "")
                    if path and path not in seen:
                        meta = domain_graph.get_meta(path)
                        eff_imp = meta.get("effective_importance", 0.5) if meta else 0.5
                        candidates.append({"path": path, "effective_importance": eff_imp})
                        seen.add(path)

            # Fill remaining with top by importance — only when no targeted results
            if not candidates:
                top_results = domain_graph.top_notes(limit=max_notes)
                for r in top_results:
                    candidates.append(r)
                    if len(candidates) >= max_notes:
                        break

            # Build snippets
            for c in candidates:
                if total_chars >= max_chars:
                    break
                content = domain_store.read(c["path"])
                if not content:
                    continue
                from extensions.memory.frontmatter import strip_frontmatter

                body = strip_frontmatter(content)
                snippet = self._truncate_note(body, 500)
                snippet_with_meta = (
                    f"**[{domain_name}] {c['path']}** (imp={c['effective_importance']})\n{snippet}"
                )
                if total_chars + len(snippet_with_meta) > max_chars:
                    break
                snippets.append(snippet_with_meta)
                total_chars += len(snippet_with_meta)

        if not snippets:
            return None

        self._injection_cache[session_id] = (now, snippets)
        return SessionOverrides(
            extra_system_prompt=[
                "## KNOWLEDGE CONTEXT (auto-injected)\n" + "\n---\n".join(snippets)
            ]
        )

    @staticmethod
    def _truncate_note(body: str, max_chars: int = 500) -> str:
        lines = body.strip().splitlines()
        result = []
        total = 0
        for line in lines:
            if total + len(line) > max_chars:
                result.append("...")
                break
            result.append(line)
            total += len(line) + 1
            if total > 50 and line.strip() == "":
                break
        return "\n".join(result)

    # -- delivery callback (reflection, domain-scoped) ----------------------

    async def _on_delivery(self, session_id: str, text: str, metadata: dict) -> None:
        if not metadata.get("is_final"):
            return
        if metadata.get("is_heartbeat") or metadata.get("is_subagent"):
            return
        if not self._domain_manager:
            return

        # Determine session's domains
        session = self.sm.sessions.get(session_id)
        if not session:
            return
        context = getattr(session, "context", {}) or {}
        domains = context.get("domains", [])
        if not domains:
            return

        # Rate limit: 1 per session per 5 min
        now = time.time()
        last = self._last_reflect.get(session_id, 0)
        if now - last < 300:
            return
        self._last_reflect[session_id] = now

        if len(self._last_reflect) > 100:
            stale = [k for k, ts in self._last_reflect.items() if now - ts > 3600]
            for k in stale:
                del self._last_reflect[k]

        asyncio.create_task(self._safe_reflect(session_id, text, metadata, domains))

    async def _safe_reflect(
        self, session_id: str, text: str, metadata: dict, domains: list[str]
    ) -> None:
        try:
            for domain_name in domains:
                domain_graph = self._domain_manager.get_graph(domain_name)
                domain_store = self._domain_manager.get_store(domain_name)
                domain_config = self._domain_manager.get_config(domain_name)
                if not domain_graph or not domain_store:
                    continue

                reflection_config = domain_config.get("reflection", {})
                if not reflection_config:
                    continue

                from extensions.memory.reflect import ReflectionEngine

                reflector = ReflectionEngine(domain_graph, config=reflection_config)

                # L1: deterministic
                actions = reflector.reflect(session_id, text, metadata)
                if actions:
                    applied = reflector.apply(actions, store=domain_store)
                    log.info(
                        "L1 reflection [%s] for session %s: %d actions proposed, %d applied",
                        domain_name,
                        session_id[:8],
                        len(actions),
                        applied,
                    )

                # L2: LLM-assisted (conditional)
                if reflector.should_trigger_l2(text):
                    await self._run_l2_reflection(
                        session_id, text, domain_name, domain_graph, domain_store, reflector
                    )

        except Exception:
            log.exception("Reflection failed for session %s", session_id[:8])

    async def _run_l2_reflection(
        self, session_id, text, domain_name, domain_graph, domain_store, reflector
    ) -> None:
        existing_notes = []
        top = domain_graph.top_notes(limit=30, min_importance=0.1)
        for n in top:
            meta = domain_graph.get_meta(n["path"])
            if meta:
                existing_notes.append(meta)

        prompt = reflector.build_l2_prompt(text, existing_notes)
        l2_model = reflector.config.get("llm_model", "claude-sonnet-4-6")
        log.info(
            "L2 reflection [%s]: calling %s for session %s", domain_name, l2_model, session_id[:8]
        )

        try:
            response = await self.engine.ask(prompt, model=l2_model, max_turns=1, timeout=60)
        except Exception:
            log.exception("L2 reflection: engine.ask failed")
            return

        if response.startswith("[Error]"):
            log.warning("L2 reflection: %s", response)
            return

        actions = reflector.parse_l2_response(response)
        if actions:
            applied = reflector.apply(actions, store=domain_store)
            log.info(
                "L2 reflection [%s] for session %s: %d actions proposed, %d applied",
                domain_name,
                session_id[:8],
                len(actions),
                applied,
            )

    # -- seed files ---------------------------------------------------------

    def _seed_files(self, memory_dir: Path) -> None:
        pass
