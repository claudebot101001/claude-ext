#!/usr/bin/env python3
"""Deduplicate Historical Instances in vuln-*.md knowledge notes.

Scans all vuln notes, finds duplicate entries within the Historical Instances
section (by report ID like #31458), and removes duplicates keeping only the first.

Usage:
    python scripts/dedup_historical.py --dry-run   # Preview changes
    python scripts/dedup_historical.py              # Apply dedup
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from extensions.memory.frontmatter import parse_frontmatter, serialize_frontmatter  # noqa: E402

MEMORY_DIR = Path.home() / ".claude-ext" / "memory"


def extract_report_ids(instance_block: str) -> list[tuple[str, str]]:
    """Extract (report_id, full_entry) pairs from a Historical Instances block.

    Each entry starts with '- Protocol:' and may have continuation lines
    starting with '  - '.
    """
    entries: list[tuple[str, str]] = []
    current_lines: list[str] = []

    for line in instance_block.splitlines():
        if line.startswith("- Protocol:"):
            if current_lines:
                entry = "\n".join(current_lines)
                rid = _extract_rid(entry)
                entries.append((rid, entry))
            current_lines = [line]
        elif line.startswith("  - ") and current_lines:
            current_lines.append(line)
        elif line.strip() == "" and current_lines:
            # blank line — flush current entry
            entry = "\n".join(current_lines)
            rid = _extract_rid(entry)
            entries.append((rid, entry))
            current_lines = []
        elif current_lines:
            current_lines.append(line)

    if current_lines:
        entry = "\n".join(current_lines)
        rid = _extract_rid(entry)
        entries.append((rid, entry))

    return entries


def _extract_rid(text: str) -> str:
    """Extract report ID like '#31458' from an entry."""
    m = re.search(r"#(\d+)", text)
    return m.group(0) if m else ""


def dedup_note(filepath: Path, dry_run: bool) -> tuple[int, int]:
    """Deduplicate Historical Instances in a single note.

    Returns (total_entries, removed_count).
    """
    text = filepath.read_text(encoding="utf-8")

    if "## Historical Instances" not in text:
        return 0, 0

    meta, body = parse_frontmatter(text)

    # Split at Historical Instances section
    parts = body.split("## Historical Instances", 1)
    before = parts[0]
    after = parts[1]

    # Find the next ## heading (if any) to bound the section
    next_heading = re.search(r"\n## ", after)
    if next_heading:
        hist_block = after[: next_heading.start()]
        remainder = after[next_heading.start() :]
    else:
        hist_block = after
        remainder = ""

    entries = extract_report_ids(hist_block)
    if not entries:
        return 0, 0

    # Deduplicate by report_id, keeping first occurrence
    seen: set[str] = set()
    unique: list[str] = []
    removed = 0

    for rid, entry in entries:
        key = rid if rid else entry  # fallback to full text if no report_id
        if key in seen:
            removed += 1
        else:
            seen.add(key)
            unique.append(entry)

    if removed == 0:
        return len(entries), 0

    # Rebuild section
    new_hist = "\n" + "\n\n".join(unique) + "\n"
    new_body = before + "## Historical Instances" + new_hist + remainder

    if not dry_run:
        full = serialize_frontmatter(meta, new_body)
        filepath.write_text(full, encoding="utf-8")

    return len(entries), removed


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Deduplicate historical instances")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    topics_dir = MEMORY_DIR / "topics"
    total_files = 0
    total_entries = 0
    total_removed = 0

    for path in sorted(topics_dir.glob("vuln-*.md")):
        entries, removed = dedup_note(path, args.dry_run)
        if removed > 0:
            tag = "(dry-run) " if args.dry_run else ""
            print(f"{tag}{path.name}: {entries} entries, removed {removed} duplicates")
            total_removed += removed
        total_entries += entries
        total_files += 1

    print(
        f"\n{'DRY RUN — ' if args.dry_run else ''}Summary: "
        f"{total_files} notes, {total_entries} entries, {total_removed} duplicates removed"
    )


if __name__ == "__main__":
    main()
