#!/usr/bin/env python3
"""Memory MCP server — three-layer identity + knowledge graph + persistent store.

Spawned by Claude Code per session.  Inherits MCPServerBase for protocol
handling.  Regular memory ops use direct file I/O via MEMORY_DIR env var.
Personality ops use bridge RPC for encryption/decryption via main process.
Knowledge graph ops use KnowledgeGraph (own SQLite connection to shared DB).
"""

import json
import os
import sys
from pathlib import Path

# Ensure the project root is importable (mcp_server.py lives in extensions/memory/)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402
from extensions.memory.frontmatter import (  # noqa: E402
    NoteMeta,
    Relation,
    merge_meta,
    parse_frontmatter,
    serialize_frontmatter,
    strip_frontmatter,
    validate_relation_type,
)
from extensions.memory.graph import KnowledgeGraph  # noqa: E402
from extensions.memory.store import MemoryStore  # noqa: E402

# Paths that the AI cannot write to (human-authored only)
_READONLY_PATHS = {"constitution.md"}

_RECOMMENDED_RELATION_TYPES = (
    "Recommended types: related, depends_on, similar_to, caused_by, "
    "exploits, mitigates, composes_with, shares_pattern. "
    "Any lowercase alphanumeric + underscore format accepted (e.g. 'my_custom_type')."
)


class MemoryMCPServer(MCPServerBase):
    name = "memory"
    gateway_description = (
        "Cross-session memory, identity, and knowledge graph "
        "(read/write/search files, metadata, relations, personality). action='help' for details."
    )
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
                "Supports YAML frontmatter for structured metadata. "
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
                        "description": "Full content to write (may include YAML frontmatter)",
                    },
                    "expires": {
                        "type": "string",
                        "description": "Optional ISO 8601 expiry timestamp.",
                    },
                    "meta": {
                        "type": "object",
                        "description": (
                            "Optional inline metadata (tags, keywords, importance). "
                            "If provided, overwrites any frontmatter in content."
                        ),
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
                        "description": "Relative path to the memory file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to append",
                    },
                    "expires": {
                        "type": "string",
                        "description": "Optional ISO 8601 expiry timestamp.",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "memory_search",
            "description": (
                "Search memory files by keyword/regex. Supports tag and importance filtering. "
                "Returns sections ranked by BM25 * importance."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keyword or regex pattern)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter: notes matching ANY of these tags",
                    },
                    "min_importance": {
                        "type": "number",
                        "description": "Filter: minimum effective importance (0.0-1.0)",
                    },
                    "include_related": {
                        "type": "boolean",
                        "description": "Also return 1-hop related notes (default false)",
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
        # -- Knowledge Graph tools --
        {
            "name": "memory_meta",
            "description": (
                "Get or set structured metadata for a note (tags, keywords, importance). "
                "action='get' returns merged view (frontmatter + access tracking). "
                "action='set' updates frontmatter and SQLite."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "set"],
                        "description": "Action to perform",
                    },
                    "path": {"type": "string", "description": "Relative path to the note"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "(set) Tags to assign",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "(set) Keywords for similarity matching",
                    },
                    "importance": {
                        "type": "number",
                        "description": "(set) Base importance 0.0-1.0",
                    },
                },
                "required": ["action", "path"],
            },
        },
        {
            "name": "memory_relate",
            "description": (
                "Manage knowledge graph edges between notes. " + _RECOMMENDED_RELATION_TYPES
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "list"],
                        "description": "Action to perform",
                    },
                    "source": {"type": "string", "description": "Source note path"},
                    "target": {
                        "type": "string",
                        "description": "(add/remove) Target note path",
                    },
                    "type": {
                        "type": "string",
                        "description": f"(add/remove) Relation type. {_RECOMMENDED_RELATION_TYPES}",
                    },
                    "weight": {
                        "type": "number",
                        "description": "(add) Edge weight, default 1.0",
                    },
                },
                "required": ["action", "source"],
            },
        },
        {
            "name": "memory_graph",
            "description": (
                "Graph traversal and analysis. Actions: neighbors (BFS traversal), "
                "suggest_links (Jaccard keyword similarity), list_tags, stats."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["neighbors", "suggest_links", "list_tags", "stats"],
                        "description": "Action to perform",
                    },
                    "path": {
                        "type": "string",
                        "description": "(neighbors/suggest_links) Note path",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "(neighbors) BFS depth 1-3, default 1",
                    },
                    "rel_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "(neighbors) Filter by relation types",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "memory_import",
            "description": (
                "Batch import: write content + set metadata + add relations in one call. "
                "Efficient for bulk knowledge ingestion (e.g. writeup processing)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the note"},
                    "content": {
                        "type": "string",
                        "description": "Note content (body, no frontmatter)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags to assign",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keywords for similarity matching",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Base importance 0.0-1.0, default 0.5",
                    },
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string"},
                                "type": {"type": "string"},
                                "weight": {"type": "number"},
                            },
                            "required": ["target", "type"],
                        },
                        "description": "Relations to create from this note",
                    },
                },
                "required": ["path", "content"],
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
            "memory_meta": self._handle_meta,
            "memory_relate": self._handle_relate,
            "memory_graph": self._handle_graph,
            "memory_import": self._handle_import,
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

    def _get_graph(self) -> KnowledgeGraph:
        if not hasattr(self, "_graph"):
            memory_dir = os.environ.get("MEMORY_DIR", "")
            if not memory_dir:
                raise RuntimeError("MEMORY_DIR not set")
            self._graph = KnowledgeGraph(Path(memory_dir))
        return self._graph

    @staticmethod
    def _check_readonly(path: str) -> str | None:
        """Return error string if path is read-only, else None."""
        normalized = os.path.normpath(path.strip()).lower()
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
        # Touch for access tracking (SQLite only, no file write)
        store.touch(path)
        return content

    def _handle_write(self, args: dict) -> str:
        path = args.get("path")
        content = args.get("content")
        expires = args.get("expires")
        inline_meta = args.get("meta")
        if not path:
            return "Error: 'path' is required."
        if content is None:
            return "Error: 'content' is required."
        err = self._check_readonly(path)
        if err:
            return err

        # If inline meta provided, strip existing frontmatter and rebuild
        if inline_meta and isinstance(inline_meta, dict):
            body = strip_frontmatter(content)
            meta = NoteMeta()
            meta = merge_meta(meta, inline_meta)
            content = serialize_frontmatter(meta, body)

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

        filter_tags = args.get("tags")
        min_importance = args.get("min_importance", 0.0)
        include_related = args.get("include_related", False)

        store = self._get_store()
        try:
            results = store.search(query)
        except ValueError as e:
            return f"Error: {e}"
        if not results:
            return "No matches found."

        # Enrich with graph data if filtering requested
        graph = self._get_graph()
        enriched = []
        seen_files = set()

        for r in results:
            file_path = r.get("file", "")
            meta = graph.get_meta(file_path)

            # Tag filter
            if filter_tags and meta and not set(meta.get("tags", [])) & set(filter_tags):
                continue

            # Importance filter
            eff_imp = meta.get("effective_importance", 0.5) if meta else 0.5
            if eff_imp < min_importance:
                continue

            r["effective_importance"] = round(eff_imp, 4)
            enriched.append(r)
            seen_files.add(file_path)

        # Include 1-hop related notes
        if include_related:
            for file_path in list(seen_files):
                neighbors = graph.neighbors(file_path, depth=1)
                for n in neighbors:
                    if n["path"] not in seen_files:
                        enriched.append(
                            {
                                "file": n["path"],
                                "heading": "(related)",
                                "snippet": f"via {n['via_type']} from {file_path}",
                                "via_relation": True,
                            }
                        )
                        seen_files.add(n["path"])

        if not enriched:
            return "No matches found (after filtering)."

        lines = []
        for r in enriched:
            imp_str = (
                f" [imp={r.get('effective_importance', '?')}]"
                if "effective_importance" in r
                else ""
            )
            rel_str = " (related)" if r.get("via_relation") else ""
            if "heading" in r:
                lines.append(
                    f"{r['file']} [{r['heading']}]{imp_str}{rel_str}: {r.get('snippet', '')}"
                )
            else:
                lines.append(f"{r['file']}:{r.get('line', '?')}{imp_str}: {r.get('text', '')}")
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

    # -- knowledge graph handlers -------------------------------------------

    def _handle_meta(self, args: dict) -> str:
        action = args.get("action")
        path = args.get("path")
        if not path:
            return "Error: 'path' is required."

        graph = self._get_graph()

        if action == "get":
            meta = graph.get_meta(path)
            if not meta:
                return f"No metadata found for {path}"
            return json.dumps(meta, indent=2)

        elif action == "set":
            err = self._check_readonly(path)
            if err:
                return err
            # Update SQLite
            graph.set_meta(
                path,
                importance=args.get("importance"),
                keywords=args.get("keywords"),
                tags=args.get("tags"),
            )
            # Also update frontmatter in the .md file
            store = self._get_store()
            content = store.read(path)
            if content is not None:
                existing_meta, body = parse_frontmatter(content)
                updates = {}
                if "tags" in args:
                    updates["tags"] = args["tags"]
                if "keywords" in args:
                    updates["keywords"] = args["keywords"]
                if "importance" in args:
                    updates["importance"] = args["importance"]
                new_meta = merge_meta(existing_meta, updates)
                new_content = serialize_frontmatter(new_meta, body)
                try:
                    store.write(path, new_content)
                except ValueError as e:
                    return f"SQLite updated but frontmatter write failed: {e}"
            return f"Metadata updated for {path}"

        return f"Error: Unknown action '{action}'. Use 'get' or 'set'."

    def _handle_relate(self, args: dict) -> str:
        action = args.get("action")
        source = args.get("source")
        if not source:
            return "Error: 'source' is required."

        graph = self._get_graph()

        if action == "list":
            rels = graph.get_relations(source)
            if not rels:
                return f"No relations for {source}"
            return json.dumps(rels, indent=2)

        target = args.get("target")
        rel_type = args.get("type")
        if not target:
            return "Error: 'target' is required for add/remove."
        if not rel_type:
            return "Error: 'type' is required for add/remove."
        if not validate_relation_type(rel_type):
            return f"Error: Invalid relation type format '{rel_type}'. Must match ^[a-z][a-z0-9_]{{0,49}}$"

        if action == "add":
            weight = args.get("weight", 1.0)
            ok = graph.add_relation(source, target, rel_type, weight)
            return (
                f"Relation added: {source} --[{rel_type}]--> {target}"
                if ok
                else "Error: failed to add relation"
            )

        elif action == "remove":
            ok = graph.remove_relation(source, target, rel_type)
            return "Relation removed" if ok else "No such relation found"

        return f"Error: Unknown action '{action}'. Use 'add', 'remove', or 'list'."

    def _handle_graph(self, args: dict) -> str:
        action = args.get("action")
        graph = self._get_graph()

        if action == "neighbors":
            path = args.get("path")
            if not path:
                return "Error: 'path' is required for neighbors."
            depth = args.get("depth", 1)
            rel_types = args.get("rel_types")
            results = graph.neighbors(path, depth=depth, rel_types=rel_types)
            if not results:
                return f"No neighbors found for {path}"
            return json.dumps(results, indent=2)

        elif action == "suggest_links":
            path = args.get("path")
            if not path:
                return "Error: 'path' is required for suggest_links."
            suggestions = graph.suggest_links(path)
            if not suggestions:
                return f"No link suggestions for {path}"
            return json.dumps(suggestions, indent=2)

        elif action == "list_tags":
            tags = graph.list_tags()
            if not tags:
                return "No tags found."
            return json.dumps(tags, indent=2)

        elif action == "stats":
            return json.dumps(graph.stats(), indent=2)

        return f"Error: Unknown action '{action}'. Use 'neighbors', 'suggest_links', 'list_tags', or 'stats'."

    def _handle_import(self, args: dict) -> str:
        """Batch import: write + meta + relations in one call."""
        path = args.get("path")
        content = args.get("content")
        if not path:
            return "Error: 'path' is required."
        if content is None:
            return "Error: 'content' is required."
        err = self._check_readonly(path)
        if err:
            return err

        # Build frontmatter
        meta = NoteMeta(
            tags=args.get("tags", []),
            keywords=args.get("keywords", []),
            importance=args.get("importance", 0.5),
        )

        # Add relations to frontmatter
        raw_rels = args.get("relations", [])
        for r in raw_rels:
            if (
                isinstance(r, dict)
                and r.get("target")
                and r.get("type")
                and validate_relation_type(r["type"])
            ):
                meta.relations.append(
                    Relation(
                        target=r["target"],
                        type=r["type"],
                        weight=r.get("weight", 1.0),
                    )
                )

        full_content = serialize_frontmatter(meta, content)

        # Write file (triggers indexing + graph sync)
        store = self._get_store()
        try:
            nbytes = store.write(path, full_content)
        except ValueError as e:
            return f"Error: {e}"

        # Also add relations directly to graph (for relations targeting notes
        # that might not be in frontmatter's outgoing edges)
        graph = self._get_graph()
        for r in raw_rels:
            if (
                isinstance(r, dict)
                and r.get("target")
                and r.get("type")
                and validate_relation_type(r["type"])
            ):
                graph.add_relation(path, r["target"], r["type"], r.get("weight", 1.0))

        parts = [f"Imported {nbytes} bytes to {path}"]
        if meta.tags:
            parts.append(f"tags={meta.tags}")
        if meta.keywords:
            parts.append(f"keywords={meta.keywords}")
        if meta.relations:
            parts.append(f"relations={len(meta.relations)}")
        return ", ".join(parts)

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
