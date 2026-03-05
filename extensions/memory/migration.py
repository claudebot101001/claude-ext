"""One-time migration from old memory format (v1) to three-layer identity (v2).

Migration steps:
1. Archive daily/ logs into topics/daily-archive.md, remove daily/
2. Seed constitution.md (human-editable, AI read-only)
3. Create users/ and events/ directories
4. Write .migrated_v2 marker
"""

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATION_MARKER = ".migrated_v2"


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
