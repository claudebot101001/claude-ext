"""Tests for MAGMA knowledge graph: frontmatter, graph, reflect, migration v3."""

import json
import sqlite3

import pytest

from extensions.memory.frontmatter import (
    NoteMeta,
    Relation,
    merge_meta,
    parse_frontmatter,
    serialize_frontmatter,
    strip_frontmatter,
    validate_relation_type,
)
from extensions.memory.graph import KnowledgeGraph, effective_importance
from extensions.memory.reflect import ReflectionEngine
from extensions.memory.store import MemoryStore


@pytest.fixture
def memory_dir(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def store(memory_dir):
    return MemoryStore(memory_dir)


@pytest.fixture
def graph(memory_dir):
    """KnowledgeGraph with tables created by MemoryStore."""
    # Create store first to init schema
    s = MemoryStore(memory_dir)
    g = KnowledgeGraph(memory_dir)
    yield g
    g.close()
    s.close()


# ===========================================================================
# Frontmatter
# ===========================================================================


class TestFrontmatter:
    def test_parse_no_frontmatter(self):
        meta, body = parse_frontmatter("# Hello\nWorld")
        assert meta.tags == []
        assert meta.importance == 0.5
        assert body == "# Hello\nWorld"

    def test_parse_with_frontmatter(self):
        text = "---\ntags: [reentrancy, defi]\nimportance: 0.8\n---\n# Note\nContent"
        meta, body = parse_frontmatter(text)
        assert meta.tags == ["reentrancy", "defi"]
        assert meta.importance == 0.8
        assert body == "# Note\nContent"

    def test_parse_with_keywords_and_relations(self):
        text = (
            "---\n"
            "keywords: [delegatecall, proxy]\n"
            "relations:\n"
            "  - target: topics/flash-loans.md\n"
            "    type: related\n"
            "---\n"
            "Body"
        )
        meta, body = parse_frontmatter(text)
        assert meta.keywords == ["delegatecall", "proxy"]
        assert len(meta.relations) == 1
        assert meta.relations[0].target == "topics/flash-loans.md"
        assert meta.relations[0].type == "related"
        assert body == "Body"

    def test_parse_malformed_yaml(self):
        text = "---\n: invalid: yaml: [[\n---\nBody"
        meta, body = parse_frontmatter(text)
        assert meta.importance == 0.5  # defaults
        assert body == text  # returns full text

    def test_parse_importance_clamped(self):
        text = "---\nimportance: 5.0\n---\nBody"
        meta, _ = parse_frontmatter(text)
        assert meta.importance == 1.0

        text2 = "---\nimportance: -1.0\n---\nBody"
        meta2, _ = parse_frontmatter(text2)
        assert meta2.importance == 0.0

    def test_parse_invalid_relation_type_skipped(self):
        text = "---\nrelations:\n  - target: a.md\n    type: INVALID-TYPE\n---\nBody"
        meta, _ = parse_frontmatter(text)
        assert len(meta.relations) == 0

    def test_serialize_empty_meta(self):
        meta = NoteMeta()
        result = serialize_frontmatter(meta, "# Hello")
        assert result == "# Hello"  # No frontmatter for defaults

    def test_serialize_with_tags(self):
        meta = NoteMeta(tags=["defi", "audit"])
        result = serialize_frontmatter(meta, "# Note")
        assert result.startswith("---\n")
        assert "tags:" in result
        assert "# Note" in result

    def test_roundtrip(self):
        meta = NoteMeta(
            tags=["a", "b"],
            keywords=["kw1"],
            importance=0.7,
            relations=[Relation(target="other.md", type="related")],
        )
        text = serialize_frontmatter(meta, "Body content")
        parsed_meta, body = parse_frontmatter(text)
        assert parsed_meta.tags == meta.tags
        assert parsed_meta.keywords == meta.keywords
        assert parsed_meta.importance == meta.importance
        assert body == "Body content"

    def test_strip_frontmatter(self):
        text = "---\ntags: [a]\n---\nBody"
        assert strip_frontmatter(text) == "Body"

    def test_strip_no_frontmatter(self):
        text = "Just body"
        assert strip_frontmatter(text) == "Just body"


class TestMergeMeta:
    def test_merge_partial_update(self):
        existing = NoteMeta(tags=["old"], importance=0.3)
        result = merge_meta(existing, {"tags": ["new1", "new2"]})
        assert result.tags == ["new1", "new2"]
        assert result.importance == 0.3  # unchanged

    def test_merge_importance(self):
        existing = NoteMeta()
        result = merge_meta(existing, {"importance": 0.9})
        assert result.importance == 0.9

    def test_merge_no_updates(self):
        existing = NoteMeta(tags=["keep"], importance=0.7)
        result = merge_meta(existing, {})
        assert result.tags == ["keep"]
        assert result.importance == 0.7


class TestRelationType:
    def test_valid_types(self):
        assert validate_relation_type("related") is True
        assert validate_relation_type("depends_on") is True
        assert validate_relation_type("shares_pattern") is True
        assert validate_relation_type("exploits") is True

    def test_invalid_types(self):
        assert validate_relation_type("UPPERCASE") is False
        assert validate_relation_type("has-dash") is False
        assert validate_relation_type("") is False
        assert validate_relation_type("a" * 51) is False
        assert validate_relation_type("123start") is False


# ===========================================================================
# Effective Importance / Decay
# ===========================================================================


class TestEffectiveImportance:
    def test_no_access(self):
        assert effective_importance(0.5, "", 0) == 0.5

    def test_recent_access_boosted(self):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        result = effective_importance(0.5, now, 5)
        assert result > 0.5  # freq boost

    def test_old_access_decayed(self):
        result = effective_importance(0.5, "2020-01-01T00:00:00+00:00", 1)
        assert result < 0.5  # decayed

    def test_clamped_to_1(self):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        result = effective_importance(1.0, now, 100)
        assert result <= 1.0


# ===========================================================================
# KnowledgeGraph
# ===========================================================================


class TestKnowledgeGraph:
    def test_set_and_get_meta(self, graph):
        graph.set_meta("test.md", importance=0.8, keywords=["kw1"], tags=["tag1"])
        meta = graph.get_meta("test.md")
        assert meta is not None
        assert meta["importance"] == 0.8
        assert meta["keywords"] == ["kw1"]
        assert meta["tags"] == ["tag1"]

    def test_get_meta_nonexistent(self, graph):
        assert graph.get_meta("nonexistent.md") is None

    def test_set_meta_importance_zero(self, graph):
        """Regression: importance=0.0 should not fall back to 0.5."""
        graph.set_meta("zero.md", importance=0.0)
        meta = graph.get_meta("zero.md")
        assert meta is not None
        assert meta["importance"] == 0.0

    def test_touch(self, graph):
        graph.set_meta("test.md", importance=0.5)
        meta1 = graph.get_meta("test.md")
        graph.touch("test.md")
        meta2 = graph.get_meta("test.md")
        assert meta2["access_count"] == meta1["access_count"] + 1

    def test_boost_importance(self, graph):
        graph.set_meta("test.md", importance=0.5)
        new_val = graph.boost_importance("test.md", 0.1)
        assert new_val == 0.6
        meta = graph.get_meta("test.md")
        assert meta["importance"] == 0.6

    def test_boost_importance_clamped(self, graph):
        graph.set_meta("test.md", importance=0.95)
        new_val = graph.boost_importance("test.md", 0.1)
        assert new_val == 1.0

    def test_add_and_get_relation(self, graph):
        ok = graph.add_relation("a.md", "b.md", "related")
        assert ok is True
        rels = graph.get_relations("a.md")
        assert len(rels) == 1
        assert rels[0]["target"] == "b.md"

    def test_relation_invalid_type(self, graph):
        ok = graph.add_relation("a.md", "b.md", "INVALID")
        assert ok is False

    def test_remove_relation(self, graph):
        graph.add_relation("a.md", "b.md", "related")
        ok = graph.remove_relation("a.md", "b.md", "related")
        assert ok is True
        assert graph.get_relations("a.md") == []

    def test_neighbors(self, graph):
        graph.add_relation("a.md", "b.md", "related")
        graph.add_relation("b.md", "c.md", "depends_on")
        n1 = graph.neighbors("a.md", depth=1)
        assert len(n1) == 1
        assert n1[0]["path"] == "b.md"

        n2 = graph.neighbors("a.md", depth=2)
        assert len(n2) == 2

    def test_neighbors_with_type_filter(self, graph):
        graph.add_relation("a.md", "b.md", "related")
        graph.add_relation("a.md", "c.md", "depends_on")
        n = graph.neighbors("a.md", rel_types=["depends_on"])
        assert len(n) == 1
        assert n[0]["path"] == "c.md"

    def test_suggest_links(self, graph):
        graph.set_meta("a.md", keywords=["delegatecall", "proxy", "storage"])
        graph.set_meta("b.md", keywords=["delegatecall", "storage", "collision"])
        graph.set_meta("c.md", keywords=["erc20", "transfer"])

        suggestions = graph.suggest_links("a.md", threshold=0.2)
        assert len(suggestions) >= 1
        assert suggestions[0]["target"] == "b.md"
        assert "delegatecall" in suggestions[0]["shared_keywords"]

    def test_suggest_links_excludes_existing(self, graph):
        graph.set_meta("a.md", keywords=["kw1", "kw2"])
        graph.set_meta("b.md", keywords=["kw1", "kw2"])
        graph.add_relation("a.md", "b.md", "related")
        suggestions = graph.suggest_links("a.md")
        assert all(s["target"] != "b.md" for s in suggestions)

    def test_list_tags(self, graph):
        graph.set_meta("a.md", tags=["defi", "audit"])
        graph.set_meta("b.md", tags=["defi"])
        tags = graph.list_tags()
        assert tags[0]["tag"] == "defi"
        assert tags[0]["count"] == 2

    def test_stats(self, graph):
        graph.set_meta("a.md", tags=["t1"])
        graph.add_relation("a.md", "b.md", "related")
        s = graph.stats()
        assert s["notes"] >= 1
        assert s["relations"] >= 1

    def test_top_notes(self, graph):
        graph.set_meta("a.md", importance=0.9, tags=["audit"])
        graph.set_meta("b.md", importance=0.3, tags=["general"])
        top = graph.top_notes(limit=10)
        assert top[0]["path"] == "a.md"

    def test_top_notes_tag_filter(self, graph):
        graph.set_meta("a.md", importance=0.9, tags=["audit"])
        graph.set_meta("b.md", importance=0.8, tags=["general"])
        top = graph.top_notes(tags=["audit"])
        assert len(top) == 1
        assert top[0]["path"] == "a.md"

    def test_sync_from_frontmatter(self, graph):
        meta = NoteMeta(
            tags=["tag1"],
            keywords=["kw1"],
            importance=0.7,
            relations=[Relation(target="b.md", type="related")],
        )
        graph.sync_from_frontmatter("a.md", meta)
        result = graph.get_meta("a.md")
        assert result["importance"] == 0.7
        assert result["tags"] == ["tag1"]
        rels = graph.get_relations("a.md")
        assert len(rels) == 1

    def test_remove_note(self, graph):
        graph.set_meta("a.md", tags=["t"])
        graph.add_relation("a.md", "b.md", "related")
        graph.remove_note("a.md")
        assert graph.get_meta("a.md") is None
        assert graph.get_relations("a.md") == []


# ===========================================================================
# Store Integration (frontmatter indexing)
# ===========================================================================


class TestStoreGraphIntegration:
    def test_write_with_frontmatter_indexes_meta(self, store, memory_dir):
        content = "---\ntags: [test]\nkeywords: [kw1]\nimportance: 0.8\n---\n# Hello"
        store.write("note.md", content)

        # Force index
        store._index.ensure_index()

        # Check that note_meta was populated
        db = sqlite3.connect(str(memory_dir / ".search_index.db"))
        row = db.execute("SELECT importance FROM note_meta WHERE path = ?", ("note.md",)).fetchone()
        db.close()
        assert row is not None
        assert row[0] == 0.8

    def test_write_plain_file_gets_default_meta(self, store, memory_dir):
        store.write("plain.md", "# No frontmatter")
        store._index.ensure_index()

        db = sqlite3.connect(str(memory_dir / ".search_index.db"))
        row = db.execute(
            "SELECT importance FROM note_meta WHERE path = ?", ("plain.md",)
        ).fetchone()
        db.close()
        assert row is not None
        assert row[0] == 0.5

    def test_delete_cascades_graph(self, store, memory_dir):
        store.write("del.md", "---\ntags: [x]\n---\n# To delete")
        store._index.ensure_index()

        # Delete file and re-index
        (memory_dir / "del.md").unlink()
        store._index.ensure_index()

        db = sqlite3.connect(str(memory_dir / ".search_index.db"))
        row = db.execute("SELECT path FROM note_meta WHERE path = ?", ("del.md",)).fetchone()
        tags = db.execute("SELECT tag FROM note_tags WHERE path = ?", ("del.md",)).fetchall()
        db.close()
        assert row is None
        assert tags == []

    def test_touch_updates_access(self, store, memory_dir):
        store.write("touch.md", "# Test")
        store._index.ensure_index()
        store.touch("touch.md")

        db = sqlite3.connect(str(memory_dir / ".search_index.db"))
        row = db.execute(
            "SELECT access_count FROM note_meta WHERE path = ?", ("touch.md",)
        ).fetchone()
        db.close()
        assert row is not None
        assert row[0] == 1

    def test_api_relation_survives_reindex(self, store, memory_dir):
        """API-added relations should not be deleted on file re-index."""
        from extensions.memory.graph import KnowledgeGraph

        store.write("a.md", "# Note A")
        store._index.ensure_index()

        # Add relation via API (not frontmatter)
        graph = KnowledgeGraph(memory_dir)
        graph.add_relation("a.md", "b.md", "related")

        # Re-write the file (triggers re-index)
        store.write("a.md", "# Note A updated")
        store._index.ensure_index()

        # API relation should survive
        db = sqlite3.connect(str(memory_dir / ".search_index.db"))
        row = db.execute(
            "SELECT origin FROM note_relations WHERE source = ? AND target = ?",
            ("a.md", "b.md"),
        ).fetchone()
        db.close()
        graph.close()
        assert row is not None
        assert row[0] == "api"


# ===========================================================================
# Reflection Engine
# ===========================================================================


class TestReflectionEngine:
    def test_reflect_empty_text(self, graph):
        engine = ReflectionEngine(graph)
        actions = engine.reflect("s1", "", {})
        assert actions == []

    def test_reflect_mentions_paths(self, graph):
        graph.set_meta("topics/flash-loans.md", keywords=["flash", "loan"])
        engine = ReflectionEngine(graph)
        text = "The analysis referenced topics/flash-loans.md and found issues."
        actions = engine.reflect("s1", text, {})
        # Should have a BoostImportance for the mentioned path
        boost_actions = [a for a in actions if a.kind == "boost"]
        assert len(boost_actions) == 1
        assert boost_actions[0].path == "topics/flash-loans.md"

    def test_reflect_suggests_relations(self, graph):
        graph.set_meta("topics/a.md", keywords=["kw"])
        graph.set_meta("topics/b.md", keywords=["kw"])
        engine = ReflectionEngine(graph)
        text = "Comparing topics/a.md and topics/b.md reveals similar patterns."
        actions = engine.reflect("s1", text, {})
        rel_actions = [a for a in actions if a.kind == "suggest_relation"]
        assert len(rel_actions) == 1

    def test_apply_boost(self, graph):
        graph.set_meta("topics/test.md", importance=0.5)
        engine = ReflectionEngine(graph)
        text = "Referenced topics/test.md here."
        actions = engine.reflect("s1", text, {})
        applied = engine.apply(actions)
        assert applied >= 1
        meta = graph.get_meta("topics/test.md")
        assert meta["importance"] > 0.5

    def test_l2_trigger_disabled_by_default(self, graph):
        engine = ReflectionEngine(graph)
        assert engine.should_trigger_l2("s1", "found vulnerability", {}) is False

    def test_l2_trigger_enabled(self, graph):
        engine = ReflectionEngine(graph, config={"llm_enabled": True})
        assert engine.should_trigger_l2("s1", "found reentrancy vulnerability", {}) is True
        assert engine.should_trigger_l2("s1", "just a normal task", {}) is False


# ===========================================================================
# Migration v3
# ===========================================================================


class TestMigrationV3:
    def test_needs_migration(self, memory_dir):
        from extensions.memory.migration import needs_migration_v3

        assert needs_migration_v3(memory_dir) is True
        (memory_dir / ".migrated_v3").write_text("v3\n")
        assert needs_migration_v3(memory_dir) is False

    def test_migrate_seeds_meta(self, memory_dir):
        from extensions.memory.migration import migrate_v3

        # Create schema first (normally done by MemoryStore)
        _ = MemoryStore(memory_dir)

        # Create some files
        (memory_dir / "topics").mkdir(exist_ok=True)
        (memory_dir / "topics" / "test.md").write_text("# Test")
        (memory_dir / "general.md").write_text("# General")

        migrate_v3(memory_dir)

        # Check note_meta was seeded
        db = sqlite3.connect(str(memory_dir / ".search_index.db"))
        rows = db.execute("SELECT path, importance FROM note_meta").fetchall()
        db.close()

        meta_dict = {r[0]: r[1] for r in rows}
        assert "general.md" in meta_dict
        assert meta_dict["general.md"] == 0.9
        assert "topics/test.md" in meta_dict
        assert meta_dict["topics/test.md"] == 0.5

    def test_migrate_idempotent(self, memory_dir):
        from extensions.memory.migration import migrate_v3

        _ = MemoryStore(memory_dir)
        (memory_dir / "general.md").write_text("# G")
        migrate_v3(memory_dir)
        migrate_v3(memory_dir)  # second call should be no-op
        assert (memory_dir / ".migrated_v3").exists()
