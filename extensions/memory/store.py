"""Persistent memory store backed by Markdown files on disk.

Provides path-safe, flock-protected read/write/append/search operations
on a directory of .md files.  Designed for Claude agents to maintain
cross-session knowledge.

Thread/process safety: unified lockfile (memory.lock).  Read-only ops
take LOCK_SH; mutations hold LOCK_EX.  Writes use atomic temp+rename.

Search uses FTS5 full-text indexing (BM25 ranking, Porter stemming) when
available, with automatic fallback to regex line-by-line scan.  The FTS5
index is a derived cache stored at .search_index.db — Markdown files
remain the sole source of truth.

Design decision: direct file I/O (no bridge RPC).  Memory is plaintext
Markdown with no encryption or access-control needs — unlike vault, there
is no security benefit to routing through the main process.  MCP server
processes hold their own MemoryStore instances and read/write directly.
If audit logging is needed later, add it here rather than introducing a
bridge layer.
"""

import contextlib
import fcntl
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_ALLOWED_EXTENSION = ".md"
_MAX_READ_SIZE = 512 * 1024  # 512 KB
_MAX_SEARCH_RESULTS = 50
_LOCK_FILE = "memory.lock"
_INDEX_DB = ".search_index.db"
_EXPIRY_FILE = ".expiry.json"
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
_CODE_FENCE_RE = re.compile(r"^```")
_SNIPPET_MAX = 500
_REGEX_META_RE = re.compile(
    r"\\[dDwWsSbB]"  # escape sequences like \d, \w, \s
    r"|\.\*|\.\+"  # .* or .+
    r"|\[[^\]]+\]"  # character classes [abc]
    r"|\([^)]*\)"  # groups (...)
    r"|\|"  # alternation
)


# ---------------------------------------------------------------------------
# FTS5 search index — derived cache over Markdown files
# ---------------------------------------------------------------------------


class MemoryIndex:
    """SQLite FTS5 index over memory Markdown files.

    The index is a rebuildable cache; Markdown files are the source of truth.
    Multiple processes can share the same DB safely via WAL mode.
    """

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self._db_path = memory_dir / _INDEX_DB
        self.available = False
        self._db: sqlite3.Connection | None = None
        try:
            self._open_db()
            self.available = True
        except sqlite3.OperationalError:
            log.warning("FTS5 not available in sqlite3, using regex fallback")

    def _open_db(self) -> None:
        """Open DB and initialize schema. Handles corrupt DB recovery."""
        try:
            self._db = sqlite3.connect(str(self._db_path))
            self._init_pragmas()
            self._init_schema()
        except sqlite3.DatabaseError:
            log.warning("Corrupt search index, rebuilding...")
            if self._db:
                self._db.close()
            self._db_path.unlink(missing_ok=True)
            self._db = sqlite3.connect(str(self._db_path))
            self._init_pragmas()
            self._init_schema()

    def _init_pragmas(self) -> None:
        assert self._db is not None
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA synchronous=NORMAL")

    def _init_schema(self) -> None:
        assert self._db is not None
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS file_meta (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                path UNINDEXED,
                heading,
                content,
                tokenize='porter unicode61'
            );
            """
        )

    # -- index maintenance ---------------------------------------------------

    def ensure_index(self) -> None:
        """Sync index with files on disk: re-index stale, add new, remove orphans."""
        if not self.available or self._db is None:
            return

        # Collect current file state
        disk_files: dict[str, int] = {}
        for filepath in sorted(self.memory_dir.rglob("*.md")):
            if not filepath.is_file() or filepath.name == _LOCK_FILE:
                continue
            rel = str(filepath.relative_to(self.memory_dir))
            try:
                disk_files[rel] = filepath.stat().st_mtime_ns
            except OSError:
                continue

        # Collect indexed file state
        indexed: dict[str, int] = {}
        for row in self._db.execute("SELECT path, mtime_ns FROM file_meta"):
            indexed[row[0]] = row[1]

        # Remove orphans (files deleted from disk)
        orphans = set(indexed) - set(disk_files)
        for path in orphans:
            self._remove_file(path)

        # Re-index stale and new files
        for path, mtime_ns in disk_files.items():
            if path not in indexed or indexed[path] != mtime_ns:
                self._index_file(path, mtime_ns)

    def invalidate_file(self, path: str) -> None:
        """Mark a file as stale so next ensure_index() re-indexes it."""
        if not self.available or self._db is None:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO file_meta (path, mtime_ns) VALUES (?, 0)",
                (path,),
            )
            self._db.commit()
        except sqlite3.Error:
            pass  # best-effort

    def search(self, fts_query: str, limit: int = _MAX_SEARCH_RESULTS) -> list[dict]:
        """Search chunks using FTS5 MATCH with BM25 ranking.

        Returns list of {"file", "heading", "snippet", "rank"}.
        """
        if not self.available or self._db is None:
            return []

        rows = self._db.execute(
            """
            SELECT path, heading, content,
                   bm25(chunks, 0.0, 2.0, 1.0) AS rank,
                   highlight(chunks, 2, char(2), char(3)) AS highlighted
            FROM chunks
            WHERE chunks MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()

        results = []
        for path, heading, content, rank, highlighted in rows:
            snippet = _extract_snippet(content, highlighted)
            results.append(
                {
                    "file": path,
                    "heading": heading,
                    "snippet": snippet,
                    "rank": rank,
                }
            )
        return results

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    # -- internal helpers ----------------------------------------------------

    def _index_file(self, rel_path: str, mtime_ns: int) -> None:
        """Chunk a Markdown file and insert into FTS5."""
        assert self._db is not None
        filepath = self.memory_dir / rel_path
        try:
            text = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return

        # Remove old chunks
        self._remove_file(rel_path, update_meta=False)

        # Chunk and insert
        chunks = _chunk_markdown(text)
        for heading, chunk_text in chunks:
            self._db.execute(
                "INSERT INTO chunks (path, heading, content) VALUES (?, ?, ?)",
                (rel_path, heading, chunk_text),
            )

        # Update mtime
        self._db.execute(
            "INSERT OR REPLACE INTO file_meta (path, mtime_ns) VALUES (?, ?)",
            (rel_path, mtime_ns),
        )
        self._db.commit()

    def _remove_file(self, rel_path: str, update_meta: bool = True) -> None:
        """Remove all chunks and metadata for a file."""
        assert self._db is not None
        self._db.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
        if update_meta:
            self._db.execute("DELETE FROM file_meta WHERE path = ?", (rel_path,))
            self._db.commit()


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------


def _chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split Markdown text into (heading, content) chunks.

    Strategy:
    - Split by headings (H1-H4)
    - Code blocks are kept as atomic units within their section
    - Files without headings are split by blank-line groups
    """
    lines = text.splitlines()

    # Check if file has any headings
    has_headings = any(_HEADING_RE.match(line) for line in lines)
    if not has_headings:
        return _chunk_by_paragraphs(text)

    chunks: list[tuple[str, str]] = []
    current_heading = "(top)"
    current_lines: list[str] = []
    in_code_block = False

    for line in lines:
        if _CODE_FENCE_RE.match(line):
            in_code_block = not in_code_block
            current_lines.append(line)
            continue

        if in_code_block:
            current_lines.append(line)
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            # Flush previous chunk
            if current_lines:
                chunk_text = "\n".join(current_lines).strip()
                if chunk_text:
                    chunks.append((current_heading, chunk_text))
            current_heading = heading_match.group(0)
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last chunk
    if current_lines:
        chunk_text = "\n".join(current_lines).strip()
        if chunk_text:
            chunks.append((current_heading, chunk_text))

    return chunks if chunks else [("(top)", text.strip())]


def _chunk_by_paragraphs(text: str) -> list[tuple[str, str]]:
    """Split non-heading text into chunks by blank-line groups."""
    sections = re.split(r"\n\s*\n", text)
    chunks = []
    for section in sections:
        stripped = section.strip()
        if stripped:
            # Use first line as heading hint
            first_line = stripped.split("\n")[0][:80]
            chunks.append((first_line, stripped))
    return chunks if chunks else [("(top)", text.strip())]


def _extract_snippet(content: str, highlighted: str) -> str:
    """Extract a smart snippet from content using FTS5 highlight markers."""
    if len(content) <= _SNIPPET_MAX:
        return content

    # Find match position in highlighted text (STX=\x02, ETX=\x03),
    # then map back to content by subtracting marker character offsets.
    marker_pos = highlighted.find("\x02")
    if marker_pos < 0:
        return content[:_SNIPPET_MAX] + "..."

    # Count marker characters before the match position to get true offset
    markers_before = 0
    for i in range(marker_pos):
        if highlighted[i] in ("\x02", "\x03"):
            markers_before += 1
    pos = marker_pos - markers_before

    # Extract window centered on first match
    half = _SNIPPET_MAX // 2
    start = max(0, pos - half)
    end = min(len(content), pos + half)

    # Snap to line boundaries
    if start > 0:
        nl = content.find("\n", start)
        if nl >= 0 and nl < pos:
            start = nl + 1
    if end < len(content):
        nl = content.rfind("\n", pos, end)
        if nl >= 0:
            end = nl

    snippet = content[start:end].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


class MemoryStore:
    """Markdown-on-disk memory store with path safety and file locking.

    Usage::

        store = MemoryStore(Path("~/.claude-ext/memory"))
        store.write("MEMORY.md", "# Memory\\n...")
        print(store.read("MEMORY.md"))
        store.append("daily/2025-01-15.md", "- learned X")
        results = store.search("pytest")
    """

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.memory_dir / _LOCK_FILE
        self._index = MemoryIndex(memory_dir)
        log.info("MemoryStore initialized at %s (FTS5: %s)", memory_dir, self._index.available)

    def close(self) -> None:
        """Close the FTS5 index DB connection."""
        self._index.close()

    # -- public API ---------------------------------------------------------

    def read(self, path: str) -> str | None:
        """Read a memory file. Returns None if not found."""
        resolved = self._safe_resolve(path)
        with self._shared_lock():
            if not resolved.exists() or not resolved.is_file():
                return None
            size = resolved.stat().st_size
            if size > _MAX_READ_SIZE:
                log.warning("File %s exceeds max read size (%d > %d)", path, size, _MAX_READ_SIZE)
            return resolved.read_text(encoding="utf-8")[:_MAX_READ_SIZE]

    def write(self, path: str, content: str, expires: str | None = None) -> int:
        """Atomically overwrite a memory file. Creates parent dirs as needed.

        Args:
            path: Relative path to the memory file.
            content: Full content to write.
            expires: Optional ISO 8601 expiry timestamp. File will be eligible
                     for cleanup after this time. Pass None to clear any
                     existing expiry.

        Returns the number of bytes written.
        """
        resolved = self._safe_resolve(path)
        with self._exclusive_lock():
            resolved.parent.mkdir(parents=True, exist_ok=True)
            tmp = resolved.with_suffix(".md.tmp")
            data = content.encode("utf-8")
            tmp.write_bytes(data)
            tmp.rename(resolved)
            if expires:
                self._set_expiry_locked(path, expires)
            else:
                self._clear_expiry_locked(path)
        self._index.invalidate_file(path)
        log.info("Memory: wrote %d bytes to %s", len(data), path)
        return len(data)

    def append(
        self, path: str, content: str, timestamp: bool = True, expires: str | None = None
    ) -> int:
        """Append content to a memory file with optional UTC timestamp.

        Creates the file and parent dirs if they don't exist.

        Args:
            path: Relative path to the memory file.
            content: Content to append.
            timestamp: Whether to prepend a UTC timestamp line.
            expires: Optional ISO 8601 expiry timestamp. Sets or updates
                     the file's expiry. Only the latest expiry value is kept.

        Returns the number of bytes appended.
        """
        resolved = self._safe_resolve(path)
        with self._exclusive_lock():
            resolved.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if timestamp:
                ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                lines.append(f"\n[{ts}]\n")
            lines.append(content)
            if not content.endswith("\n"):
                lines.append("\n")
            chunk = "".join(lines)
            data = chunk.encode("utf-8")
            with open(resolved, "a", encoding="utf-8") as f:
                f.write(chunk)
            if expires:
                self._set_expiry_locked(path, expires)
        self._index.invalidate_file(path)
        log.info("Memory: appended %d bytes to %s", len(data), path)
        return len(data)

    def cleanup_expired(self) -> list[str]:
        """Remove memory files whose expiry timestamp has passed.

        Returns list of deleted relative paths.  Safe to call frequently;
        no-op when nothing is expired.
        """
        now = datetime.now(UTC)
        deleted: list[str] = []
        with self._exclusive_lock():
            expiry_map = self._read_expiry_locked()
            if not expiry_map:
                return []
            remaining = {}
            for path, ts_str in expiry_map.items():
                try:
                    expires_at = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    log.warning("Invalid expiry timestamp for %s: %s, removing entry", path, ts_str)
                    continue
                if expires_at <= now:
                    # Delete the file if it exists
                    try:
                        resolved = self._safe_resolve(path)
                        if resolved.exists():
                            resolved.unlink()
                            self._index.invalidate_file(path)
                            log.info("Memory: expired and deleted %s", path)
                        deleted.append(path)
                    except (ValueError, OSError) as e:
                        log.warning("Failed to delete expired file %s: %s", path, e)
                else:
                    remaining[path] = ts_str
            self._write_expiry_locked(remaining)
        return deleted

    def get_expiry(self, path: str) -> str | None:
        """Return the ISO 8601 expiry timestamp for a file, or None."""
        with self._shared_lock():
            expiry_map = self._read_expiry_locked()
            return expiry_map.get(path)

    # -- expiry internals (must be called under lock) -----------------------

    def _read_expiry_locked(self) -> dict[str, str]:
        """Read the expiry map from disk. Returns empty dict on any error."""
        expiry_path = self.memory_dir / _EXPIRY_FILE
        if not expiry_path.exists():
            return {}
        try:
            data = expiry_path.read_text(encoding="utf-8")
            return json.loads(data) if data.strip() else {}
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt expiry file, resetting")
            return {}

    def _write_expiry_locked(self, expiry_map: dict[str, str]) -> None:
        """Atomically write the expiry map to disk."""
        expiry_path = self.memory_dir / _EXPIRY_FILE
        if not expiry_map:
            expiry_path.unlink(missing_ok=True)
            return
        tmp = expiry_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(expiry_map, indent=2), encoding="utf-8")
        tmp.rename(expiry_path)

    def _set_expiry_locked(self, path: str, expires: str) -> None:
        """Set expiry for a path. Validates the timestamp format."""
        # Validate ISO 8601
        try:
            datetime.fromisoformat(expires)
        except ValueError as e:
            raise ValueError(f"Invalid expiry timestamp: {expires!r} ({e})") from e
        expiry_map = self._read_expiry_locked()
        expiry_map[path] = expires
        self._write_expiry_locked(expiry_map)

    def _clear_expiry_locked(self, path: str) -> None:
        """Remove expiry for a path (if any)."""
        expiry_map = self._read_expiry_locked()
        if path in expiry_map:
            del expiry_map[path]
            self._write_expiry_locked(expiry_map)

    def search(self, query: str, glob_pattern: str = "**/*.md") -> list[dict]:
        """Search memory files by keyword (FTS5) or regex pattern.

        Uses FTS5 full-text search with BM25 ranking when available.
        Falls back to regex line-by-line scan for regex patterns or when
        FTS5 is unavailable.

        FTS5 results: list of {"file", "heading", "snippet", "rank"}.
        Regex results: list of {"file", "line", "text"}.
        """
        # 1. Regex metacharacters → legacy regex search
        if _is_regex_pattern(query):
            return self._regex_search(query, glob_pattern)

        # 2. FTS5 search with BM25 ranking
        if self._index.available:
            self._index.ensure_index()
            try:
                return self._index.search(_fts5_quote(query))
            except sqlite3.OperationalError:
                log.debug("FTS5 query failed, falling back to regex")

        # 3. Fallback: regex search (FTS5 unavailable or query failed)
        return self._regex_search(query, glob_pattern)

    def list_files(self, subdir: str = "") -> list[dict]:
        """List .md files, sorted by modification time (newest first).

        Returns list of {"path": relative_path, "size": bytes, "modified": iso_timestamp}.
        """
        if subdir:
            target = self._safe_resolve_dir(subdir)
        else:
            target = self.memory_dir

        entries = []
        with self._shared_lock():
            if not target.exists() or not target.is_dir():
                return []
            for filepath in target.rglob("*.md"):
                if not filepath.is_file():
                    continue
                if filepath.name == _LOCK_FILE:
                    continue
                try:
                    st = filepath.stat()
                except OSError:
                    continue
                rel = str(filepath.relative_to(self.memory_dir))
                entries.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                    }
                )
        entries.sort(key=lambda e: e["modified"], reverse=True)
        return entries

    @staticmethod
    def today_log_path() -> str:
        """Return the conventional path for today's daily log."""
        return f"daily/{datetime.now(UTC).strftime('%Y-%m-%d')}.md"

    # -- internal search helpers --------------------------------------------

    def _regex_search(self, query: str, glob_pattern: str = "**/*.md") -> list[dict]:
        """Original regex line-by-line search (legacy fallback)."""
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Invalid regex: {e}") from e

        results = []
        with self._shared_lock():
            for filepath in sorted(self.memory_dir.glob(glob_pattern)):
                if not filepath.is_file():
                    continue
                if filepath.suffix != _ALLOWED_EXTENSION:
                    continue
                if filepath.name == _LOCK_FILE:
                    continue
                rel = str(filepath.relative_to(self.memory_dir))
                try:
                    text = filepath.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        results.append(
                            {
                                "file": rel,
                                "line": lineno,
                                "text": line.rstrip(),
                            }
                        )
                        if len(results) >= _MAX_SEARCH_RESULTS:
                            return results
        return results

    # -- path safety (core security boundary) --------------------------------

    def _safe_resolve(self, path: str) -> Path:
        """Resolve a relative path safely within memory_dir.

        Raises ValueError on any path traversal or policy violation.
        """
        if not path:
            raise ValueError("Empty path")
        if path.startswith("/"):
            raise ValueError("Absolute paths not allowed")
        if ".." in Path(path).parts:
            raise ValueError("Path traversal not allowed")

        resolved = (self.memory_dir / path).resolve()

        if not resolved.is_relative_to(self.memory_dir.resolve()):
            raise ValueError("Path escapes memory directory")

        if resolved.suffix != _ALLOWED_EXTENSION:
            raise ValueError(f"Only {_ALLOWED_EXTENSION} files allowed, got: {resolved.suffix!r}")

        if resolved.name == _LOCK_FILE:
            raise ValueError("Cannot access lock file")

        # Note: symlink escapes are already caught by the is_relative_to check
        # above, since .resolve() follows symlinks before the comparison.

        return resolved

    def _safe_resolve_dir(self, subdir: str) -> Path:
        """Resolve a subdirectory path safely within memory_dir."""
        if not subdir:
            return self.memory_dir
        if subdir.startswith("/"):
            raise ValueError("Absolute paths not allowed")
        if ".." in Path(subdir).parts:
            raise ValueError("Path traversal not allowed")

        resolved = (self.memory_dir / subdir).resolve()
        if not resolved.is_relative_to(self.memory_dir.resolve()):
            raise ValueError("Path escapes memory directory")

        return resolved

    # -- locking ------------------------------------------------------------

    @contextlib.contextmanager
    def _shared_lock(self):
        """LOCK_SH on the unified lockfile."""
        f = open(self._lock_path, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    @contextlib.contextmanager
    def _exclusive_lock(self):
        """LOCK_EX on the unified lockfile."""
        f = open(self._lock_path, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _is_regex_pattern(query: str) -> bool:
    """Detect if a query contains regex metacharacters."""
    return bool(_REGEX_META_RE.search(query))


def _fts5_quote(query: str) -> str:
    """Quote tokens to prevent FTS5 operator injection (NOT/OR/AND/NEAR).

    Also escapes embedded double quotes ('"' → '""' in FTS5).
    """
    tokens = query.split()
    if not tokens:
        return '""'
    return " ".join(f'"{t.replace(chr(34), chr(34) + chr(34))}"' for t in tokens)
