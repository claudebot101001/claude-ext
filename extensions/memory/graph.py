"""Knowledge graph over memory notes — CRUD, traversal, decay, link suggestion.

Owns its own SQLite connection to the shared WAL-mode search_index.db.
Tables: note_meta, note_tags, note_relations (created by MemoryIndex._init_schema).
"""

import json
import logging
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from extensions.memory.frontmatter import NoteMeta, validate_relation_type

log = logging.getLogger(__name__)

_INDEX_DB = ".search_index.db"


def effective_importance(
    base: float,
    accessed: str,
    access_count: int,
    half_life_days: float = 30.0,
) -> float:
    """Compute time-decayed importance with frequency boost.

    base * decay * freq_boost, clamped to [0, 1].
    """
    if not accessed:
        return base

    now = datetime.now(UTC)
    try:
        accessed_dt = datetime.fromisoformat(accessed.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return base

    age_days = max(0, (now - accessed_dt).total_seconds() / 86400)
    decay = 0.5 ** (age_days / half_life_days)
    freq_boost = min(1.5, 1.0 + math.log1p(access_count) / math.log1p(10))
    return min(1.0, base * decay * freq_boost)


class KnowledgeGraph:
    """Graph operations over memory note metadata.

    Opens its own DB connection (WAL mode, shared cache).
    Thread-safe for concurrent reads; writes are serialized by SQLite WAL.
    """

    def __init__(self, memory_dir: Path, half_life_days: float = 30.0):
        self.memory_dir = memory_dir
        self.half_life_days = half_life_days
        self._db_path = memory_dir / _INDEX_DB
        self._db: sqlite3.Connection | None = None
        try:
            self._db = sqlite3.connect(str(self._db_path))
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=NORMAL")
            log.info("KnowledgeGraph connected to %s", self._db_path)
        except sqlite3.Error:
            log.exception("Failed to open KnowledgeGraph DB")

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    # -- meta CRUD -----------------------------------------------------------

    def get_meta(self, path: str) -> dict | None:
        """Get merged metadata: frontmatter fields + SQLite access tracking."""
        if not self._db:
            return None
        row = self._db.execute(
            "SELECT importance, created, accessed, access_count, keywords FROM note_meta WHERE path = ?",
            (path,),
        ).fetchone()
        if not row:
            return None

        importance, created, accessed, access_count, keywords_json = row
        try:
            keywords = json.loads(keywords_json) if keywords_json else []
        except json.JSONDecodeError:
            keywords = []

        tags = [r[0] for r in self._db.execute("SELECT tag FROM note_tags WHERE path = ?", (path,))]

        eff_imp = effective_importance(importance, accessed, access_count, self.half_life_days)

        return {
            "path": path,
            "importance": importance,
            "effective_importance": round(eff_imp, 4),
            "created": created,
            "accessed": accessed,
            "access_count": access_count,
            "keywords": keywords,
            "tags": tags,
        }

    def set_meta(
        self,
        path: str,
        importance: float | None = None,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update metadata fields in SQLite. Creates row if missing."""
        if not self._db:
            return

        # Ensure row exists
        existing = self._db.execute("SELECT path FROM note_meta WHERE path = ?", (path,)).fetchone()
        if not existing:
            now_iso = datetime.now(UTC).isoformat()
            self._db.execute(
                "INSERT INTO note_meta (path, importance, created, accessed, access_count, keywords) "
                "VALUES (?, ?, ?, ?, 0, '[]')",
                (path, importance if importance is not None else 0.5, now_iso, now_iso),
            )

        if importance is not None:
            self._db.execute(
                "UPDATE note_meta SET importance = ? WHERE path = ?",
                (max(0.0, min(1.0, importance)), path),
            )

        if keywords is not None:
            self._db.execute(
                "UPDATE note_meta SET keywords = ? WHERE path = ?",
                (json.dumps(keywords), path),
            )

        if tags is not None:
            self._db.execute("DELETE FROM note_tags WHERE path = ?", (path,))
            for tag in tags:
                tag = tag.strip()
                if tag:
                    self._db.execute(
                        "INSERT OR IGNORE INTO note_tags (path, tag) VALUES (?, ?)",
                        (path, tag),
                    )

        self._db.commit()

    def touch(self, path: str) -> None:
        """Update access timestamp and increment count. SQLite only."""
        if not self._db:
            return
        now_iso = datetime.now(UTC).isoformat()
        self._db.execute(
            "UPDATE note_meta SET accessed = ?, access_count = access_count + 1 WHERE path = ?",
            (now_iso, path),
        )
        self._db.commit()

    def boost_importance(self, path: str, delta: float = 0.05) -> float | None:
        """Increment base importance by delta (clamped to 1.0). Returns new value."""
        if not self._db:
            return None
        row = self._db.execute(
            "SELECT importance FROM note_meta WHERE path = ?", (path,)
        ).fetchone()
        if not row:
            return None
        new_val = min(1.0, row[0] + delta)
        self._db.execute("UPDATE note_meta SET importance = ? WHERE path = ?", (new_val, path))
        self._db.commit()
        return new_val

    # -- relations -----------------------------------------------------------

    def add_relation(self, source: str, target: str, rel_type: str, weight: float = 1.0) -> bool:
        """Add a directed edge. Returns True on success."""
        if not self._db:
            return False
        if not validate_relation_type(rel_type):
            log.warning("Invalid relation type: %r", rel_type)
            return False
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO note_relations (source, target, type, weight) VALUES (?, ?, ?, ?)",
                (source, target, rel_type, weight),
            )
            self._db.commit()
            return True
        except sqlite3.Error:
            log.exception("Failed to add relation %s -> %s", source, target)
            return False

    def remove_relation(self, source: str, target: str, rel_type: str) -> bool:
        """Remove a specific directed edge."""
        if not self._db:
            return False
        cursor = self._db.execute(
            "DELETE FROM note_relations WHERE source = ? AND target = ? AND type = ?",
            (source, target, rel_type),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def get_relations(self, path: str) -> list[dict]:
        """Get all relations where path is source or target."""
        if not self._db:
            return []
        rows = self._db.execute(
            "SELECT source, target, type, weight FROM note_relations WHERE source = ? OR target = ?",
            (path, path),
        ).fetchall()
        return [{"source": r[0], "target": r[1], "type": r[2], "weight": r[3]} for r in rows]

    # -- traversal -----------------------------------------------------------

    def neighbors(
        self, path: str, depth: int = 1, rel_types: list[str] | None = None
    ) -> list[dict]:
        """BFS neighbors up to depth hops. Returns list of {path, distance, via_type}."""
        if not self._db:
            return []
        depth = max(1, min(3, depth))
        visited: dict[str, dict] = {}
        frontier = {path}

        for d in range(1, depth + 1):
            next_frontier: set[str] = set()
            for node in frontier:
                query = (
                    "SELECT source, target, type FROM note_relations "
                    "WHERE (source = ? OR target = ?)"
                )
                params: list = [node, node]

                if rel_types:
                    placeholders = ",".join("?" * len(rel_types))
                    query += f" AND type IN ({placeholders})"
                    params.extend(rel_types)

                for src, tgt, rtype in self._db.execute(query, params):
                    neighbor = tgt if src == node else src
                    if neighbor != path and neighbor not in visited:
                        visited[neighbor] = {"path": neighbor, "distance": d, "via_type": rtype}
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break

        return sorted(visited.values(), key=lambda x: x["distance"])

    # -- link suggestion (Jaccard similarity on keywords) --------------------

    def suggest_links(self, path: str, threshold: float = 0.3, limit: int = 10) -> list[dict]:
        """Suggest links based on keyword Jaccard similarity.

        Returns list of {target, similarity, suggested_type} sorted by similarity desc.
        """
        if not self._db:
            return []

        # Get source keywords
        row = self._db.execute("SELECT keywords FROM note_meta WHERE path = ?", (path,)).fetchone()
        if not row:
            return []
        try:
            source_kw = set(json.loads(row[0])) if row[0] else set()
        except json.JSONDecodeError:
            source_kw = set()

        if not source_kw:
            return []

        # Get source tags for additional signal
        source_tags = {
            r[0] for r in self._db.execute("SELECT tag FROM note_tags WHERE path = ?", (path,))
        }

        # Get existing relations to exclude
        existing = {
            r[0]
            for r in self._db.execute(
                "SELECT target FROM note_relations WHERE source = ? "
                "UNION SELECT source FROM note_relations WHERE target = ?",
                (path, path),
            )
        }

        # Scan all other notes
        suggestions = []
        for other_path, other_kw_json in self._db.execute(
            "SELECT path, keywords FROM note_meta WHERE path != ?", (path,)
        ):
            if other_path in existing:
                continue
            try:
                other_kw = set(json.loads(other_kw_json)) if other_kw_json else set()
            except json.JSONDecodeError:
                continue
            if not other_kw:
                continue

            # Jaccard similarity on keywords
            intersection = source_kw & other_kw
            union = source_kw | other_kw
            kw_sim = len(intersection) / len(union) if union else 0.0

            # Tag overlap bonus
            other_tags = {
                r[0]
                for r in self._db.execute("SELECT tag FROM note_tags WHERE path = ?", (other_path,))
            }
            tag_overlap = len(source_tags & other_tags) / max(1, len(source_tags | other_tags))

            # Combined score: 70% keyword Jaccard + 30% tag overlap
            combined = 0.7 * kw_sim + 0.3 * tag_overlap

            if combined >= threshold:
                suggestions.append(
                    {
                        "target": other_path,
                        "similarity": round(combined, 4),
                        "suggested_type": "similar_to",
                        "shared_keywords": sorted(intersection),
                    }
                )

        suggestions.sort(key=lambda x: x["similarity"], reverse=True)
        return suggestions[:limit]

    # -- tag operations ------------------------------------------------------

    def list_tags(self) -> list[dict]:
        """List all tags with usage counts."""
        if not self._db:
            return []
        rows = self._db.execute(
            "SELECT tag, COUNT(*) as cnt FROM note_tags GROUP BY tag ORDER BY cnt DESC"
        ).fetchall()
        return [{"tag": r[0], "count": r[1]} for r in rows]

    # -- stats ---------------------------------------------------------------

    def stats(self) -> dict:
        """Overall graph statistics."""
        if not self._db:
            return {}
        note_count = self._db.execute("SELECT COUNT(*) FROM note_meta").fetchone()[0]
        relation_count = self._db.execute("SELECT COUNT(*) FROM note_relations").fetchone()[0]
        tag_count = self._db.execute("SELECT COUNT(DISTINCT tag) FROM note_tags").fetchone()[0]
        return {
            "notes": note_count,
            "relations": relation_count,
            "unique_tags": tag_count,
        }

    # -- top notes by effective importance -----------------------------------

    def top_notes(
        self,
        limit: int = 10,
        tags: list[str] | None = None,
        min_importance: float = 0.0,
    ) -> list[dict]:
        """Get top notes by effective importance, optionally filtered by tags."""
        if not self._db:
            return []

        if tags:
            placeholders = ",".join("?" * len(tags))
            rows = self._db.execute(
                f"SELECT DISTINCT m.path, m.importance, m.accessed, m.access_count "
                f"FROM note_meta m JOIN note_tags t ON m.path = t.path "
                f"WHERE t.tag IN ({placeholders})",
                tags,
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT path, importance, accessed, access_count FROM note_meta"
            ).fetchall()

        scored = []
        for path, importance, accessed, access_count in rows:
            eff = effective_importance(importance, accessed, access_count, self.half_life_days)
            if eff >= min_importance:
                scored.append({"path": path, "effective_importance": round(eff, 4)})

        scored.sort(key=lambda x: x["effective_importance"], reverse=True)
        return scored[:limit]

    # -- bulk sync from frontmatter ------------------------------------------

    def sync_from_frontmatter(self, path: str, meta: NoteMeta) -> None:
        """Sync frontmatter metadata to SQLite tables.

        Called during file indexing. Updates note_meta, note_tags, note_relations.
        Preserves accessed/access_count from SQLite (not in frontmatter).
        """
        if not self._db:
            return

        # Get existing access tracking
        existing = self._db.execute(
            "SELECT accessed, access_count FROM note_meta WHERE path = ?", (path,)
        ).fetchone()

        now_iso = datetime.now(UTC).isoformat()
        accessed = existing[0] if existing else now_iso
        access_count = existing[1] if existing else 0

        # Upsert note_meta
        self._db.execute(
            "INSERT OR REPLACE INTO note_meta (path, importance, created, accessed, access_count, keywords) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                path,
                meta.importance,
                meta.created or now_iso,
                accessed,
                access_count,
                json.dumps(meta.keywords),
            ),
        )

        # Sync tags
        self._db.execute("DELETE FROM note_tags WHERE path = ?", (path,))
        for tag in meta.tags:
            tag = tag.strip()
            if tag:
                self._db.execute(
                    "INSERT OR IGNORE INTO note_tags (path, tag) VALUES (?, ?)", (path, tag)
                )

        # Sync relations (only frontmatter-origin; preserve API-added relations)
        self._db.execute(
            "DELETE FROM note_relations WHERE source = ? AND origin = 'frontmatter'", (path,)
        )
        for rel in meta.relations:
            if validate_relation_type(rel.type):
                self._db.execute(
                    "INSERT OR REPLACE INTO note_relations (source, target, type, weight, origin) "
                    "VALUES (?, ?, ?, ?, 'frontmatter')",
                    (path, rel.target, rel.type, rel.weight),
                )

        self._db.commit()

    def remove_note(self, path: str) -> None:
        """Remove all graph data for a note (cascade)."""
        if not self._db:
            return
        self._db.execute("DELETE FROM note_meta WHERE path = ?", (path,))
        self._db.execute("DELETE FROM note_tags WHERE path = ?", (path,))
        self._db.execute("DELETE FROM note_relations WHERE source = ? OR target = ?", (path, path))
        self._db.commit()
