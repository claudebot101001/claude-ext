"""One-time migrations for the memory extension.

v1 → v2: Archive daily/ logs, seed constitution, create directories.
v2 → v3: Knowledge graph tables, seed note_meta from file stats.
"""

import json
import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATION_MARKER = ".migrated_v2"
_MIGRATION_V3_MARKER = ".migrated_v3"
_INDEX_DB = ".search_index.db"


def needs_migration(memory_dir: Path) -> bool:
    """Check if migration from v1 to v2 is needed."""
    return not (memory_dir / _MIGRATION_MARKER).exists()


def migrate(memory_dir: Path) -> None:
    """Run one-time v1 -> v2 migration. Idempotent (marker file)."""
    if not needs_migration(memory_dir):
        return

    log.info("Running memory v1 -> v2 migration...")

    # 1. Archive daily logs
    _archive_daily_logs(memory_dir)

    # 2. Seed constitution.md
    _seed_constitution(memory_dir)

    # 3. Create directories
    (memory_dir / "users").mkdir(exist_ok=True)
    (memory_dir / "events").mkdir(exist_ok=True)

    # 4. Write marker
    (memory_dir / _MIGRATION_MARKER).write_text("v2\n", encoding="utf-8")
    log.info("Memory migration v1 -> v2 complete")


def _archive_daily_logs(memory_dir: Path) -> None:
    """Archive daily/ logs into topics/daily-archive.md, then remove daily/."""
    daily_dir = memory_dir / "daily"
    if not daily_dir.exists():
        return
    md_files = sorted(daily_dir.rglob("*.md"))
    if not md_files:
        shutil.rmtree(daily_dir)
        return

    archive_lines = [
        "# Daily Log Archive\n",
        "Migrated from daily/ directory during v2 migration.\n",
    ]
    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8")
            archive_lines.append(f"\n## {md_file.stem}\n")
            archive_lines.append(text)
        except (OSError, UnicodeDecodeError):
            continue

    topics_dir = memory_dir / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    (topics_dir / "daily-archive.md").write_text("\n".join(archive_lines), encoding="utf-8")
    shutil.rmtree(daily_dir)
    log.info("Archived %d daily log file(s) to topics/daily-archive.md", len(md_files))


def _seed_constitution(memory_dir: Path) -> None:
    """Create seed constitution.md if it doesn't exist."""
    path = memory_dir / "constitution.md"
    if path.exists():
        return
    path.write_text(
        "# Constitution\n"
        "\n"
        "<!-- Foundational rules authored by the human operator. -->\n"
        "<!-- The AI reads this at every session start but CANNOT modify it. -->\n"
        "<!-- Edit this file directly to set your agent's core principles. -->\n",
        encoding="utf-8",
    )
    log.info("Created seed constitution.md")


# ---------------------------------------------------------------------------
# v2 → v3: Knowledge graph tables + seed note_meta
# ---------------------------------------------------------------------------


def needs_migration_v3(memory_dir: Path) -> bool:
    """Check if migration from v2 to v3 is needed."""
    return not (memory_dir / _MIGRATION_V3_MARKER).exists()


def migrate_v3(memory_dir: Path) -> None:
    """Run v2 → v3 migration: seed note_meta from file stats.

    Schema tables are created by MemoryIndex._init_schema() (CREATE IF NOT EXISTS),
    so this migration only needs to populate initial data.
    """
    if not needs_migration_v3(memory_dir):
        return

    log.info("Running memory v2 -> v3 migration (knowledge graph)...")

    db_path = memory_dir / _INDEX_DB
    if not db_path.exists():
        # DB will be created by MemoryIndex; just write marker
        (memory_dir / _MIGRATION_V3_MARKER).write_text("v3\n", encoding="utf-8")
        return

    try:
        db = sqlite3.connect(str(db_path))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        log.exception("Failed to open DB for v3 migration")
        return

    try:
        _seed_note_meta(db, memory_dir)
        db.commit()
    except sqlite3.Error:
        log.exception("v3 migration failed")
    finally:
        db.close()

    (memory_dir / _MIGRATION_V3_MARKER).write_text("v3\n", encoding="utf-8")
    log.info("Memory migration v2 -> v3 complete")


def _seed_note_meta(db: sqlite3.Connection, memory_dir: Path) -> None:
    """Populate note_meta from existing .md files' filesystem stats."""
    # Importance heuristics by path
    importance_map = {
        "constitution.md": 1.0,
        "general.md": 0.9,
    }
    dir_importance = {
        "events": 0.4,
        "users": 0.6,
        "topics": 0.5,
    }
    dir_tags = {
        "topics": "topic",
        "events": "event",
        "users": "user_profile",
    }

    for filepath in sorted(memory_dir.rglob("*.md")):
        if not filepath.is_file():
            continue
        rel = str(filepath.relative_to(memory_dir))
        if rel.startswith("."):
            continue

        # Check if already seeded
        existing = db.execute("SELECT path FROM note_meta WHERE path = ?", (rel,)).fetchone()
        if existing:
            continue

        try:
            stat = filepath.stat()
        except OSError:
            continue

        # Determine importance
        importance = importance_map.get(rel)
        if importance is None:
            parts = Path(rel).parts
            if parts:
                importance = dir_importance.get(parts[0], 0.5)
            else:
                importance = 0.5

        created = datetime.fromtimestamp(stat.st_ctime, tz=UTC).isoformat()
        accessed = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()

        db.execute(
            "INSERT OR IGNORE INTO note_meta "
            "(path, importance, created, accessed, access_count, keywords) "
            "VALUES (?, ?, ?, ?, 0, '[]')",
            (rel, importance, created, accessed),
        )

        # Infer tags from directory
        parts = Path(rel).parts
        if parts and parts[0] in dir_tags:
            tag = dir_tags[parts[0]]
            db.execute(
                "INSERT OR IGNORE INTO note_tags (path, tag) VALUES (?, ?)",
                (rel, tag),
            )

    log.info("Seeded note_meta from filesystem stats")
