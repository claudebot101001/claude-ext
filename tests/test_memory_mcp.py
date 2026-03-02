"""Tests for memory MCP server tool handlers (unit-level, no actual MCP protocol)."""

import pytest

from extensions.memory.mcp_server import MemoryMCPServer


@pytest.fixture
def memory_dir(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mcp(memory_dir, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(memory_dir))
    return MemoryMCPServer()


class TestMemoryMCPRead:
    def test_read_existing_file(self, mcp, memory_dir):
        (memory_dir / "MEMORY.md").write_text("# Hello", encoding="utf-8")
        result = mcp.handlers["memory_read"]({"path": "MEMORY.md"})
        assert result == "# Hello"

    def test_read_missing_file(self, mcp):
        result = mcp.handlers["memory_read"]({"path": "nonexistent.md"})
        assert result == "File not found."

    def test_read_missing_path_param(self, mcp):
        result = mcp.handlers["memory_read"]({})
        assert "Error" in result

    def test_read_path_traversal_blocked(self, mcp):
        result = mcp.handlers["memory_read"]({"path": "../escape.md"})
        assert "Error" in result


class TestMemoryMCPWrite:
    def test_write_creates_file(self, mcp, memory_dir):
        result = mcp.handlers["memory_write"]({"path": "test.md", "content": "hello"})
        assert "Written" in result
        assert "5 bytes" in result
        assert (memory_dir / "test.md").read_text(encoding="utf-8") == "hello"

    def test_write_missing_path(self, mcp):
        result = mcp.handlers["memory_write"]({"content": "hello"})
        assert "Error" in result

    def test_write_missing_content(self, mcp):
        result = mcp.handlers["memory_write"]({"path": "test.md"})
        assert "Error" in result

    def test_write_path_traversal_blocked(self, mcp):
        result = mcp.handlers["memory_write"]({"path": "../evil.md", "content": "x"})
        assert "Error" in result


class TestMemoryMCPAppend:
    def test_append_to_new_file(self, mcp, memory_dir):
        result = mcp.handlers["memory_append"]({"path": "log.md", "content": "entry"})
        assert "Appended" in result
        content = (memory_dir / "log.md").read_text(encoding="utf-8")
        assert "entry" in content

    def test_append_missing_path(self, mcp):
        result = mcp.handlers["memory_append"]({"content": "entry"})
        assert "Error" in result

    def test_append_missing_content(self, mcp):
        result = mcp.handlers["memory_append"]({"path": "log.md"})
        assert "Error" in result


class TestMemoryMCPSearch:
    def test_search_finds_matches(self, mcp, memory_dir):
        (memory_dir / "notes.md").write_text("use pytest\nuse mypy", encoding="utf-8")
        result = mcp.handlers["memory_search"]({"query": "pytest"})
        assert "notes.md" in result
        assert "pytest" in result

    def test_search_no_matches(self, mcp, memory_dir):
        (memory_dir / "notes.md").write_text("hello world", encoding="utf-8")
        result = mcp.handlers["memory_search"]({"query": "nonexistent"})
        assert result == "No matches found."

    def test_search_missing_query(self, mcp):
        result = mcp.handlers["memory_search"]({})
        assert "Error" in result

    def test_search_invalid_regex(self, mcp):
        # Pattern must be detected as regex by heuristic AND be invalid
        result = mcp.handlers["memory_search"]({"query": r"error \d(unclosed"})
        assert "Error" in result

    def test_search_fts5_format(self, mcp, memory_dir):
        """FTS5 results should include heading context in output."""
        (memory_dir / "guide.md").write_text(
            "# Setup\nInstall dependencies\n\n## Config\nEdit config.yaml",
            encoding="utf-8",
        )
        result = mcp.handlers["memory_search"]({"query": "config"})
        assert "guide.md" in result
        # FTS5 format: "file [heading]: snippet"
        assert "[" in result

    def test_search_regex_format(self, mcp, memory_dir):
        """Regex fallback results should use line:text format."""
        (memory_dir / "notes.md").write_text("error code 404\nok 200", encoding="utf-8")
        result = mcp.handlers["memory_search"]({"query": r"code \d+"})
        assert "notes.md:1:" in result


class TestMemoryMCPList:
    def test_list_files(self, mcp, memory_dir):
        (memory_dir / "a.md").write_text("content", encoding="utf-8")
        result = mcp.handlers["memory_list"]({})
        assert "a.md" in result

    def test_list_empty(self, mcp):
        result = mcp.handlers["memory_list"]({})
        assert result == "No files found."

    def test_list_subdir(self, mcp, memory_dir):
        topics = memory_dir / "topics"
        topics.mkdir()
        (topics / "python.md").write_text("notes", encoding="utf-8")
        result = mcp.handlers["memory_list"]({"subdir": "topics"})
        assert "topics/python.md" in result


class TestMemoryMCPNoEnv:
    def test_missing_memory_dir_env(self, monkeypatch):
        monkeypatch.delenv("MEMORY_DIR", raising=False)
        server = MemoryMCPServer()
        # RuntimeError from _get_store is caught by MCPServerBase protocol layer,
        # but at handler level it propagates. Verify it raises.
        with pytest.raises(RuntimeError, match="MEMORY_DIR"):
            server.handlers["memory_read"]({"path": "MEMORY.md"})
