"""Tests for extensions/memory/store.py — MemoryStore."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from extensions.memory.store import _MAX_SEARCH_RESULTS, MemoryStore


@pytest.fixture
def memory_dir(tmp_path):
    return tmp_path / "memory"


@pytest.fixture
def store(memory_dir):
    return MemoryStore(memory_dir)


# -- Basics -----------------------------------------------------------------


class TestMemoryStoreBasics:
    def test_write_and_read(self, store):
        store.write("MEMORY.md", "# Hello\nWorld")
        assert store.read("MEMORY.md") == "# Hello\nWorld"

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
    def test_basic_search(self, store):
        store.write("notes.md", "Python is great\nJava is okay")
        results = store.search("Python")
        assert len(results) == 1
        assert results[0]["file"] == "notes.md"
        assert results[0]["line"] == 1
        assert "Python" in results[0]["text"]

    def test_case_insensitive(self, store):
        store.write("notes.md", "Python is Great")
        results = store.search("python")
        assert len(results) == 1

    def test_regex_search(self, store):
        store.write("notes.md", "error code 404\nerror code 500\nok 200")
        results = store.search(r"error code \d+")
        assert len(results) == 2

    def test_search_across_files(self, store):
        store.write("a.md", "pytest is good")
        store.write("topics/b.md", "use pytest for testing")
        results = store.search("pytest")
        assert len(results) == 2
        files = {r["file"] for r in results}
        assert files == {"a.md", "topics/b.md"}

    def test_search_no_results(self, store):
        store.write("notes.md", "hello world")
        results = store.search("nonexistent")
        assert results == []

    def test_search_invalid_regex(self, store):
        with pytest.raises(ValueError, match="Invalid regex"):
            store.search("[invalid")

    def test_search_result_limit(self, store):
        # Create file with many matching lines
        lines = [f"match line {i}" for i in range(100)]
        store.write("big.md", "\n".join(lines))
        results = store.search("match")
        assert len(results) == _MAX_SEARCH_RESULTS

    def test_search_empty_store(self, store):
        results = store.search("anything")
        assert results == []


# -- List Files -------------------------------------------------------------


class TestMemoryStoreList:
    def test_list_all(self, store):
        store.write("MEMORY.md", "index")
        store.write("topics/python.md", "python notes")
        files = store.list_files()
        assert len(files) == 2
        paths = {f["path"] for f in files}
        assert paths == {"MEMORY.md", "topics/python.md"}

    def test_list_subdir(self, store):
        store.write("MEMORY.md", "index")
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
        store.write("MEMORY.md", "# Important: use pytest")
        store.write("topics/tools.md", "pytest and mypy are useful")
        results = store.search("pytest")
        assert len(results) == 2
