"""Persistent memory store backed by Markdown files on disk.

Provides path-safe, flock-protected read/write/append/search operations
on a directory of .md files.  Designed for Claude agents to maintain
cross-session knowledge.

Thread/process safety: unified lockfile (memory.lock).  Read-only ops
take LOCK_SH; mutations hold LOCK_EX.  Writes use atomic temp+rename.

Design decision: direct file I/O (no bridge RPC).  Memory is plaintext
Markdown with no encryption or access-control needs — unlike vault, there
is no security benefit to routing through the main process.  MCP server
processes hold their own MemoryStore instances and read/write directly.
If audit logging is needed later, add it here rather than introducing a
bridge layer.
"""

import contextlib
import fcntl
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_ALLOWED_EXTENSION = ".md"
_MAX_READ_SIZE = 512 * 1024  # 512 KB
_MAX_SEARCH_RESULTS = 50
_LOCK_FILE = "memory.lock"


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
        log.info("MemoryStore initialized at %s", self.memory_dir)

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

    def write(self, path: str, content: str) -> int:
        """Atomically overwrite a memory file. Creates parent dirs as needed.

        Returns the number of bytes written.
        """
        resolved = self._safe_resolve(path)
        with self._exclusive_lock():
            resolved.parent.mkdir(parents=True, exist_ok=True)
            tmp = resolved.with_suffix(".md.tmp")
            data = content.encode("utf-8")
            tmp.write_bytes(data)
            tmp.rename(resolved)
        log.info("Memory: wrote %d bytes to %s", len(data), path)
        return len(data)

    def append(self, path: str, content: str, timestamp: bool = True) -> int:
        """Append content to a memory file with optional UTC timestamp.

        Creates the file and parent dirs if they don't exist.
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
        log.info("Memory: appended %d bytes to %s", len(data), path)
        return len(data)

    def search(self, query: str, glob_pattern: str = "**/*.md") -> list[dict]:
        """Search memory files by regex pattern (case-insensitive).

        Returns list of {"file": relative_path, "line": line_number, "text": line_text}.
        Raises ValueError on invalid regex.
        """
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
