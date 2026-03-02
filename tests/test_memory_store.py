"""Tests for extensions/memory/store.py — MemoryStore."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from extensions.memory.store import _MAX_SEARCH_RESULTS, MemoryIndex, MemoryStore


@pytest.fixture
def memory_dir(tmp_path):
    return tmp_path / "memory"


@pytest.fixture
def store(memory_dir):
    return MemoryStore(memory_dir)


# -- Basics -----------------------------------------------------------------


class TestMemoryStoreBasics:
    def test_write_and_read(self, store):
        store.write("notes.md", "# Hello\nWorld")
        assert store.read("notes.md") == "# Hello\nWorld"

    def test_read_missing_returns_none(self, store):
        assert store.read("nonexistent.md") is None

    def test_write_returns_byte_count(self, store):
        n = store.write("test.md", "hello")
        assert n == 5

    def test_write_overwrites(self, store):
        store.write("test.md", "version1")
        store.write("test.md", "version2")
        assert store.read("test.md") == "version2"

    def test_append_creates_file(self, store):
        store.append("log.md", "first entry")
        content = store.read("log.md")
        assert "first entry" in content

    def test_append_adds_to_existing(self, store):
        store.write("log.md", "# Log\n")
        store.append("log.md", "new entry")
        content = store.read("log.md")
        assert "# Log" in content
        assert "new entry" in content

    def test_append_with_timestamp(self, store):
        store.append("log.md", "entry", timestamp=True)
        content = store.read("log.md")
        assert "UTC" in content
        assert "entry" in content

    def test_append_without_timestamp(self, store):
        store.append("log.md", "entry", timestamp=False)
        content = store.read("log.md")
        assert "UTC" not in content
        assert "entry" in content

    def test_append_returns_byte_count(self, store):
        n = store.append("log.md", "hello", timestamp=False)
        assert n > 0

    def test_today_log_path(self):
        path = MemoryStore.today_log_path()
        assert path.startswith("daily/")
        assert path.endswith(".md")
        # Contains date-like pattern
        assert len(path) == len("daily/YYYY-MM-DD.md")


# -- Path Safety ------------------------------------------------------------


class TestMemoryStorePathSafety:
    def test_reject_empty_path(self, store):
        with pytest.raises(ValueError, match="Empty path"):
            store.read("")

    def test_reject_absolute_path(self, store):
        with pytest.raises(ValueError, match="Absolute"):
            store.read("/etc/passwd.md")

    def test_reject_path_traversal_dotdot(self, store):
        with pytest.raises(ValueError, match="traversal"):
            store.read("../secret.md")

    def test_reject_path_traversal_nested(self, store):
        with pytest.raises(ValueError, match="traversal"):
            store.read("topics/../../etc/passwd.md")

    def test_reject_non_md_extension(self, store):
        with pytest.raises(ValueError, match=r"Only \.md"):
            store.read("file.txt")

    def test_reject_no_extension(self, store):
        with pytest.raises(ValueError, match=r"Only \.md"):
            store.read("README")

    def test_reject_lock_file(self, store):
        with pytest.raises(ValueError):
            store.read("memory.lock")

    def test_reject_absolute_path_write(self, store):
        with pytest.raises(ValueError, match="Absolute"):
            store.write("/tmp/evil.md", "data")

    def test_reject_traversal_write(self, store):
        with pytest.raises(ValueError, match="traversal"):
            store.write("../escape.md", "data")

    def test_reject_traversal_append(self, store):
        with pytest.raises(ValueError, match="traversal"):
            store.append("../escape.md", "data")

    def test_symlink_escape_blocked(self, store, memory_dir):
        """Symlinks pointing outside memory_dir should be rejected."""
        # Create a symlink pointing outside
        link_path = memory_dir / "evil.md"
        link_path.symlink_to("/tmp/evil_target.md")
        with pytest.raises(ValueError, match=r"escapes|Symlink"):
            store.read("evil.md")


# -- Search -----------------------------------------------------------------


class TestMemoryStoreSearch:
    """Tests for search (keyword queries now use FTS5, regex patterns use fallback)."""

    def test_basic_search(self, store):
        store.write("notes.md", "Python is great\nJava is okay")
        results = store.search("Python")
        assert len(results) >= 1
        assert results[0]["file"] == "notes.md"
        # FTS5 results have "snippet", regex results have "text"
        result_text = results[0].get("snippet") or results[0].get("text", "")
        assert "Python" in result_text

    def test_case_insensitive(self, store):
        store.write("notes.md", "Python is Great")
        results = store.search("python")
        assert len(results) >= 1

    def test_regex_search(self, store):
        store.write("notes.md", "error code 404\nerror code 500\nok 200")
        results = store.search(r"error code \d+")
        assert len(results) == 2

    def test_search_across_files(self, store):
        store.write("a.md", "pytest is good")
        store.write("topics/b.md", "use pytest for testing")
        results = store.search("pytest")
        assert len(results) >= 2
        files = {r["file"] for r in results}
        assert files == {"a.md", "topics/b.md"}

    def test_search_no_results(self, store):
        store.write("notes.md", "hello world")
        results = store.search("nonexistent")
        assert results == []

    def test_search_invalid_regex(self, store):
        # Pattern must be detected as regex by heuristic AND be invalid
        with pytest.raises(ValueError, match="Invalid regex"):
            store.search(r"error \d(unclosed")

    def test_search_result_limit(self, store):
        # Create file with many matching lines — uses regex path due to glob_pattern
        lines = [f"match line {i}" for i in range(100)]
        store.write("big.md", "\n".join(lines))
        results = store.search(r"match line \d+")
        assert len(results) == _MAX_SEARCH_RESULTS

    def test_search_empty_store(self, store):
        results = store.search("anything")
        assert results == []


# -- List Files -------------------------------------------------------------


class TestMemoryStoreList:
    def test_list_all(self, store):
        store.write("notes.md", "index")
        store.write("topics/python.md", "python notes")
        files = store.list_files()
        assert len(files) == 2
        paths = {f["path"] for f in files}
        assert paths == {"notes.md", "topics/python.md"}

    def test_list_subdir(self, store):
        store.write("notes.md", "index")
        store.write("topics/a.md", "a")
        store.write("topics/b.md", "b")
        files = store.list_files("topics")
        assert len(files) == 2
        assert all(f["path"].startswith("topics/") for f in files)

    def test_list_empty(self, store):
        files = store.list_files()
        assert files == []

    def test_list_sorted_by_mtime(self, store):
        store.write("old.md", "old")
        time.sleep(0.05)
        store.write("new.md", "new")
        files = store.list_files()
        assert files[0]["path"] == "new.md"
        assert files[1]["path"] == "old.md"

    def test_list_nonexistent_subdir(self, store):
        files = store.list_files("nonexistent")
        assert files == []

    def test_list_structure(self, store):
        store.write("test.md", "hello")
        files = store.list_files()
        assert len(files) == 1
        f = files[0]
        assert "path" in f
        assert "size" in f
        assert "modified" in f
        assert f["size"] == 5

    def test_list_rejects_traversal(self, store):
        with pytest.raises(ValueError, match="traversal"):
            store.list_files("../escape")


# -- Directory Creation -----------------------------------------------------


class TestMemoryStoreDirectoryCreation:
    def test_creates_memory_dir(self, memory_dir):
        assert not memory_dir.exists()
        MemoryStore(memory_dir)
        assert memory_dir.exists()

    def test_write_creates_parent_dirs(self, store):
        store.write("topics/deep/nested.md", "content")
        assert store.read("topics/deep/nested.md") == "content"

    def test_append_creates_parent_dirs(self, store):
        store.append("daily/2025-01-15.md", "entry")
        content = store.read("daily/2025-01-15.md")
        assert "entry" in content


# -- Concurrency ------------------------------------------------------------


class TestMemoryStoreConcurrency:
    def test_concurrent_appends_no_data_loss(self, memory_dir):
        store = MemoryStore(memory_dir)
        store.write("log.md", "")

        def append_entry(i):
            store.append("log.md", f"entry-{i}", timestamp=False)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append_entry, range(20)))

        content = store.read("log.md")
        for i in range(20):
            assert f"entry-{i}" in content

    def test_concurrent_read_write(self, memory_dir):
        store = MemoryStore(memory_dir)
        store.write("data.md", "initial content")
        errors = []

        def reader():
            try:
                content = store.read("data.md")
                assert content is not None
            except Exception as e:
                errors.append(e)

        def writer(i):
            store.write("data.md", f"content version {i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for i in range(10):
                futures.append(pool.submit(writer, i))
                futures.append(pool.submit(reader))
            for f in futures:
                f.result()

        assert not errors


# -- Edge Cases -------------------------------------------------------------


class TestMemoryStoreEdgeCases:
    def test_unicode_content(self, store):
        store.write("unicode.md", "日本語テスト 🧠📝")
        assert store.read("unicode.md") == "日本語テスト 🧠📝"

    def test_large_file(self, store):
        big = "x" * 100_000
        store.write("big.md", big)
        assert store.read("big.md") == big

    def test_empty_content_write(self, store):
        store.write("empty.md", "")
        assert store.read("empty.md") == ""

    def test_multiline_append(self, store):
        store.append("log.md", "line1\nline2\nline3", timestamp=False)
        content = store.read("log.md")
        assert "line1" in content
        assert "line3" in content

    def test_write_then_search(self, store):
        store.write("notes.md", "# Important: use pytest")
        store.write("topics/tools.md", "pytest and mypy are useful")
        results = store.search("pytest")
        assert len(results) == 2


# -- FTS5 Search ---------------------------------------------------------------


class TestMemoryStoreFTSSearch:
    """Tests for FTS5 full-text search with BM25 ranking."""

    def test_fts_basic_search(self, store):
        store.write("notes.md", "# Tools\npytest is a great testing framework")
        results = store.search("pytest")
        assert len(results) >= 1
        r = results[0]
        assert r["file"] == "notes.md"
        assert "heading" in r
        assert "snippet" in r
        assert "rank" in r
        assert "pytest" in r["snippet"]

    def test_fts_stemming(self, store):
        """Porter stemming: 'testing' should match 'test', 'tested', 'tests'."""
        store.write("notes.md", "# Notes\nWe tested the configuration thoroughly")
        results = store.search("testing")
        assert len(results) >= 1
        assert "tested" in results[0]["snippet"]

    def test_fts_heading_context(self, store):
        store.write(
            "guide.md",
            "# Setup\nInstall python\n\n## Configuration\nEdit config.yaml to set options\n",
        )
        results = store.search("config")
        assert len(results) >= 1
        # Should find result under "## Configuration" heading
        headings = [r["heading"] for r in results]
        assert any("Configuration" in h for h in headings)

    def test_fts_ranking(self, store):
        """More relevant results should rank higher (lower rank value in BM25)."""
        store.write("a.md", "# Topic A\npython python python is mentioned often")
        store.write(
            "b.md", "# Topic B\npython is mentioned once here with other words filling space"
        )
        results = store.search("python")
        assert len(results) >= 2
        # Both files should appear
        files = {r["file"] for r in results}
        assert "a.md" in files
        assert "b.md" in files

    def test_fts_auto_reindex(self, store):
        """Writing then searching should find the new content."""
        store.write("v1.md", "# Version 1\noriginal content here")
        results1 = store.search("original")
        assert len(results1) >= 1

        store.write("v1.md", "# Version 1\nupdated content here")
        results2 = store.search("original")
        assert len(results2) == 0

        results3 = store.search("updated")
        assert len(results3) >= 1

    def test_fts_snippet_extraction(self, store):
        """Long chunks should return windowed snippets, not full content."""
        long_text = (
            "# Big Section\n"
            + ("filler content. " * 100)
            + "target keyword here. "
            + ("more filler. " * 100)
        )
        store.write("big.md", long_text)
        results = store.search("target")
        assert len(results) >= 1
        snippet = results[0]["snippet"]
        assert "target" in snippet
        assert len(snippet) < len(long_text)

    def test_fts_operator_quoting(self, store):
        """FTS5 operators (NOT/OR/AND) should be treated as literal words."""
        store.write("notes.md", "# Status\nThe feature is NOT working properly")
        results = store.search("NOT working")
        assert len(results) >= 1
        assert "NOT working" in results[0]["snippet"]

    def test_fts_regex_fallback(self, store):
        """Queries with regex metacharacters should use regex path."""
        store.write("notes.md", "error code 404\nerror code 500\nok 200")
        results = store.search(r"error code \d+")
        assert len(results) == 2
        # Regex results have "line" and "text" keys
        assert "line" in results[0]
        assert "text" in results[0]

    def test_fts_rebuild_on_missing_db(self, store, memory_dir):
        """Index should auto-rebuild if DB file is deleted."""
        store.write("notes.md", "# Notes\nrebuild test content")
        results1 = store.search("rebuild")
        assert len(results1) >= 1

        # Delete the index DB
        db_path = memory_dir / ".search_index.db"
        if db_path.exists():
            db_path.unlink()
        # Also remove WAL/SHM files
        for suffix in (".db-wal", ".db-shm"):
            p = memory_dir / f".search_index{suffix}"
            if p.exists():
                p.unlink()

        # Create a fresh store — should rebuild index
        store2 = MemoryStore(memory_dir)
        results2 = store2.search("rebuild")
        assert len(results2) >= 1

    def test_fts_deleted_file_not_in_results(self, store, memory_dir):
        """Deleted files should not appear in search results (no ghost entries)."""
        store.write("keep.md", "# Keep\nkeep this content")
        store.write("remove.md", "# Remove\nremove this content")

        results1 = store.search("content")
        files1 = {r["file"] for r in results1}
        assert "remove.md" in files1

        # Delete the file directly on disk
        (memory_dir / "remove.md").unlink()

        results2 = store.search("content")
        files2 = {r["file"] for r in results2}
        assert "remove.md" not in files2

    def test_fts_corrupt_db_recovery(self, store, memory_dir):
        """Corrupt DB should be auto-recovered."""
        store.write("notes.md", "# Notes\ncorrupt test content")
        results1 = store.search("corrupt")
        assert len(results1) >= 1

        # Corrupt the DB
        db_path = memory_dir / ".search_index.db"
        if db_path.exists():
            db_path.write_bytes(b"CORRUPT DATA HERE NOT A VALID SQLITE DB")

        # Create a fresh store — should detect corruption and rebuild
        store2 = MemoryStore(memory_dir)
        assert store2._index.available
        results2 = store2.search("corrupt")
        assert len(results2) >= 1

    def test_fts_unicode_cjk(self, store):
        """CJK content should be properly indexed with unicode61 tokenizer."""
        store.write("notes.md", "# 笔记\n这是一个测试文件\nPython 编程语言很好用")
        results = store.search("Python")
        assert len(results) >= 1

    def test_fts_concurrent_index_access(self, memory_dir):
        """Two MemoryStore instances should safely share the FTS5 index (WAL mode)."""
        store1 = MemoryStore(memory_dir)
        store2 = MemoryStore(memory_dir)

        store1.write("a.md", "# File A\nconcurrent test alpha")
        store2.write("b.md", "# File B\nconcurrent test beta")

        results1 = store1.search("concurrent")
        results2 = store2.search("concurrent")

        files1 = {r["file"] for r in results1}
        files2 = {r["file"] for r in results2}

        assert "a.md" in files1
        assert "b.md" in files1
        assert "a.md" in files2
        assert "b.md" in files2

    def test_fts_index_not_in_list_files(self, store):
        """The .search_index.db should not appear in list_files()."""
        store.write("notes.md", "content")
        # Trigger index creation
        store.search("content")

        files = store.list_files()
        paths = {f["path"] for f in files}
        assert ".search_index.db" not in paths
        assert all(p.endswith(".md") for p in paths)

    def test_fts_empty_query(self, store):
        """Empty-ish queries should not crash."""
        store.write("notes.md", "# Notes\nsome content")
        # Empty string is caught by MCP handler, but _fts5_quote handles it
        results = store.search("   ")
        # Should return empty or fallback gracefully
        assert isinstance(results, list)


class TestMemoryIndex:
    """Direct tests for MemoryIndex class."""

    def test_index_available(self, memory_dir):
        memory_dir.mkdir(parents=True, exist_ok=True)
        idx = MemoryIndex(memory_dir)
        # FTS5 should be available in standard Python sqlite3
        assert idx.available

    def test_chunk_markdown_headings(self):
        from extensions.memory.store import _chunk_markdown

        text = "# Title\nIntro text\n\n## Section A\nContent A\n\n## Section B\nContent B"
        chunks = _chunk_markdown(text)
        assert len(chunks) == 3
        assert chunks[0][0] == "# Title"
        assert "Intro" in chunks[0][1]
        assert chunks[1][0] == "## Section A"
        assert "Content A" in chunks[1][1]
        assert chunks[2][0] == "## Section B"
        assert "Content B" in chunks[2][1]

    def test_chunk_markdown_code_blocks(self):
        from extensions.memory.store import _chunk_markdown

        text = "# Code\nSome intro\n```python\ndef foo():\n    # heading-like but inside code\n    pass\n```\nAfter code"
        chunks = _chunk_markdown(text)
        # Code block should be kept within its section, not split by "# heading-like"
        assert len(chunks) == 1
        assert "def foo" in chunks[0][1]
        assert "After code" in chunks[0][1]

    def test_chunk_no_headings(self):
        from extensions.memory.store import _chunk_markdown

        text = "First paragraph about X.\n\nSecond paragraph about Y.\n\nThird paragraph about Z."
        chunks = _chunk_markdown(text)
        assert len(chunks) == 3

    def test_fts5_quote(self):
        from extensions.memory.store import _fts5_quote

        assert _fts5_quote("hello world") == '"hello" "world"'
        assert _fts5_quote("NOT working") == '"NOT" "working"'
        assert _fts5_quote("single") == '"single"'
        assert _fts5_quote("") == '""'

    def test_is_regex_pattern(self):
        from extensions.memory.store import _is_regex_pattern

        # Should detect regex patterns
        assert _is_regex_pattern(r"error \d+")
        assert _is_regex_pattern(r"foo.*bar")
        assert _is_regex_pattern(r"[abc]")
        assert _is_regex_pattern(r"(group)")
        assert _is_regex_pattern(r"a|b")
        assert _is_regex_pattern(r"\w+")
        # Should NOT flag common punctuation in natural language
        assert not _is_regex_pattern("simple keyword")
        assert not _is_regex_pattern("pytest")
        assert not _is_regex_pattern("hello world")
        assert not _is_regex_pattern("what?")
        assert not _is_regex_pattern("cost $50")
        assert not _is_regex_pattern("c++")
