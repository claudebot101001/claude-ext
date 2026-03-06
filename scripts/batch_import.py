#!/usr/bin/env python3
"""Batch import vulnerability reports into the MAGMA knowledge graph.

Reads Immunefi/Cantina reports, sends each to Sonnet for pattern extraction,
and imports structured results into the memory store.

Usage:
    python scripts/batch_import.py --dry-run --limit 3    # Preview 3 reports
    python scripts/batch_import.py --limit 20              # Process 20 reports
    python scripts/batch_import.py                         # Process all Critical
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from extensions.memory.frontmatter import (  # noqa: E402
    NoteMeta,
    Relation,
    parse_frontmatter,
    serialize_frontmatter,
    validate_relation_type,
)
from extensions.memory.graph import KnowledgeGraph  # noqa: E402
from extensions.memory.store import MemoryStore  # noqa: E402

log = logging.getLogger("batch_import")

MEMORY_DIR = Path.home() / ".claude-ext" / "memory"
WRITEUPS_DIR = Path.home() / "writeups"
PROGRESS_FILE = "topics/batch-progress.md"
LOCKFILE = Path.home() / ".claude-ext" / "batch_import.lock"

# Track active child process for cleanup on signal
_active_proc: asyncio.subprocess.Process | None = None


def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and its entire process group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# Sonnet invocation
MODEL = "claude-sonnet-4-6"
MAX_TURNS = 1
TIMEOUT = 120  # seconds per report


def load_batch_prompt() -> str:
    """Load the batch processing prompt from memory."""
    path = MEMORY_DIR / "topics" / "batch-processing-prompt.md"
    if not path.exists():
        raise FileNotFoundError(f"Batch processing prompt not found at {path}")
    text = path.read_text(encoding="utf-8")
    # Strip frontmatter
    _, body = parse_frontmatter(text)
    return body


def collect_reports(severity: str = "critical", limit: int = 0) -> list[Path]:
    """Collect all report files for a given severity."""
    reports = []
    for project_dir in sorted(WRITEUPS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        sev_dir = project_dir / severity
        if not sev_dir.exists():
            continue
        for report in sorted(sev_dir.glob("*.md")):
            reports.append(report)
    if limit > 0:
        reports = reports[:limit]
    return reports


def load_existing_notes(store: MemoryStore, graph: KnowledgeGraph) -> list[dict]:
    """Load existing vulnerability notes for dedup context."""
    notes = []
    for path in sorted((MEMORY_DIR / "topics").glob("vuln-*.md")):
        rel = str(path.relative_to(MEMORY_DIR))
        meta = graph.get_meta(rel)
        if meta:
            notes.append(
                {
                    "path": rel,
                    "keywords": meta.get("keywords", []),
                    "tags": meta.get("tags", []),
                    "importance": meta.get("importance", 0.5),
                }
            )
    return notes


def load_processed_ids(store: MemoryStore) -> set[str]:
    """Load set of already-processed report IDs from progress file."""
    ids = set()
    path = MEMORY_DIR / PROGRESS_FILE
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("- ") and "#" in line:
                # Extract report ID like "#28788" from progress lines
                for word in line.split():
                    if word.startswith("#") and word[1:].isdigit():
                        ids.add(word)
                        break
    return ids


def extract_report_id(path: Path) -> str:
    """Extract report ID from filename like '28788-sc-critical-...'."""
    name = path.stem
    parts = name.split("-")
    if parts and parts[0].isdigit():
        return f"#{parts[0]}"
    return f"#{name[:20]}"


def build_sonnet_prompt(batch_prompt: str, report_content: str, existing_notes: list[dict]) -> str:
    """Build the full prompt for a single Sonnet call."""
    # Truncate very long reports
    max_report = 12000
    if len(report_content) > max_report:
        report_content = report_content[:max_report] + "\n\n... [truncated]"

    # Build existing notes context
    notes_ctx = ""
    for n in existing_notes:
        kw = ", ".join(n.get("keywords", [])[:8])
        tags = ", ".join(n.get("tags", [])[:5])
        notes_ctx += f"- {n['path']} [tags={tags}, keywords={kw}]\n"

    return f"""{batch_prompt}

---

## Existing Knowledge Notes (check for deduplication)

{notes_ctx if notes_ctx else "(none yet)"}

---

## Report to Process

{report_content}"""


def _extract_json(text: str) -> dict | None:
    """Extract JSON object from text that may contain markdown fences or prose."""
    text = text.strip()

    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object within mixed text (Sonnet sometimes adds prose before/after)
    # Use bracket matching to find the outermost balanced { ... }
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return None


async def call_sonnet(prompt: str) -> dict | None:
    """Call Sonnet via claude CLI and parse JSON response."""
    cmd = [
        "claude",
        "-p",
        "-",
        "--output-format",
        "json",
        "--model",
        MODEL,
        "--max-turns",
        str(MAX_TURNS),
    ]

    # Must unset CLAUDECODE to allow nested invocation
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    global _active_proc
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,  # isolate process group for clean kills
    )
    _active_proc = proc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()), timeout=TIMEOUT
        )
    except TimeoutError:
        _kill_proc(proc)
        await proc.wait()
        log.error("Sonnet call timed out")
        return None
    finally:
        _active_proc = None

    raw = stdout.decode().strip()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        log.error("Sonnet failed (rc=%d): %s", proc.returncode, err[:200])
        return None

    # Parse outer JSON (claude CLI envelope)
    try:
        envelope = json.loads(raw)
        if envelope.get("is_error"):
            log.error("Sonnet returned error: %s", str(envelope.get("errors", ""))[:300])
            return None
        result_text = envelope.get("result")
        if not result_text:
            log.error("Sonnet returned empty result (stop_reason=%s)", envelope.get("stop_reason"))
            return None
    except (json.JSONDecodeError, TypeError):
        result_text = raw

    # Parse inner JSON (Sonnet's structured output)
    text = result_text.strip()
    parsed = _extract_json(text)
    if parsed is None:
        log.error("Failed to parse Sonnet JSON output: %s", text[:300])
        return None

    # Handle nested JSON — Sonnet sometimes wraps the result
    if isinstance(parsed, dict) and "action" not in parsed:
        # Check common nesting patterns
        for key in ("result", "output", "response", "data"):
            if key in parsed and isinstance(parsed[key], dict) and "action" in parsed[key]:
                return parsed[key]
        # Check if there's a single dict value with action
        dict_vals = [v for v in parsed.values() if isinstance(v, dict) and "action" in v]
        if len(dict_vals) == 1:
            return dict_vals[0]
        # No action field — treat as parse failure to trigger retry
        log.warning("Sonnet JSON has no 'action' field. Keys: %s", list(parsed.keys()))
        return None

    return parsed


def apply_create(data: dict, store: MemoryStore, graph: KnowledgeGraph) -> str:
    """Apply a 'create' action — write new knowledge note."""
    path = data.get("path", "")
    if not path or not path.startswith("topics/vuln-"):
        return f"REJECTED: invalid path {path!r}"

    # Check if file already exists
    if (MEMORY_DIR / path).exists():
        return f"SKIPPED: {path} already exists"

    # Build note content
    title = data.get("title", "Untitled")
    pattern = data.get("pattern", "")
    indicators = data.get("code_indicators", [])
    attack_path = data.get("attack_path", [])
    variants = data.get("variants", [])
    hist = data.get("historical_instance", {})
    mitigation = data.get("mitigation", [])

    body_parts = [f"# {title}\n"]

    body_parts.append("## Pattern")
    body_parts.append(pattern + "\n")

    if indicators:
        body_parts.append("## Key Code Indicators")
        for ind in indicators:
            body_parts.append(f"- {ind}")
        body_parts.append("")

    if attack_path:
        body_parts.append("## Attack Path")
        for i, step in enumerate(attack_path, 1):
            body_parts.append(f"{i}. {step}")
        body_parts.append("")

    if variants:
        body_parts.append("## Variants")
        for v in variants:
            body_parts.append(f"- {v}")
        body_parts.append("")

    body_parts.append("## Historical Instances")
    if hist:
        proto = hist.get("protocol", "Unknown")
        contract = hist.get("contract", "")
        sev = hist.get("severity", "Critical")
        rid = hist.get("report_id", "")
        desc = hist.get("description", "")
        label = f"{proto} ({contract})" if contract else proto
        body_parts.append(f"- Protocol: {label}, Severity: {sev}, Report: {rid}")
        if desc:
            body_parts.append(f"  - {desc}")
    body_parts.append("")

    if mitigation:
        body_parts.append("## Mitigation")
        for m in mitigation:
            body_parts.append(f"- {m}")
        body_parts.append("")

    content = "\n".join(body_parts)

    # Build metadata
    tags = [str(t) for t in data.get("tags", [])]
    keywords = [str(k) for k in data.get("keywords", [])]
    importance = float(data.get("importance", 0.9))
    relations = []
    for r in data.get("relations", []):
        target = r.get("target", "")
        rel_type = r.get("type", "related")
        if target and validate_relation_type(rel_type):
            relations.append(Relation(target=target, type=rel_type))

    meta = NoteMeta(tags=tags, keywords=keywords, importance=importance, relations=relations)
    full = serialize_frontmatter(meta, content)
    store.write(path, full)

    # Add relations to graph (in addition to frontmatter)
    for r in data.get("relations", []):
        target = r.get("target", "")
        rel_type = r.get("type", "related")
        if target and validate_relation_type(rel_type):
            graph.add_relation(path, target, rel_type)

    return f"CREATED: {path} ({len(tags)} tags, {len(keywords)} keywords)"


def apply_append(data: dict, store: MemoryStore, graph: KnowledgeGraph) -> str:
    """Apply an 'append' action — add variant to existing note."""
    existing_path = data.get("existing_path", "")
    if not existing_path:
        return "REJECTED: no existing_path"

    if not existing_path.startswith("topics/vuln-"):
        return f"REJECTED: invalid path {existing_path!r}"

    # Read through store (validates path safety)
    text = store.read(existing_path)
    if text is None:
        return f"REJECTED: {existing_path} does not exist"
    meta, body = parse_frontmatter(text)

    # Add new historical instance (skip if report_id already present)
    hist = data.get("historical_instance", {})
    if hist:
        rid = hist.get("report_id", "")
        if rid and rid in body:
            return f"SKIPPED: {existing_path} already contains {rid}"

        proto = hist.get("protocol", "Unknown")
        contract = hist.get("contract", "")
        sev = hist.get("severity", "Critical")
        desc = hist.get("description", "")
        label = f"{proto} ({contract})" if contract else proto
        instance_line = f"\n- Protocol: {label}, Severity: {sev}, Report: {rid}"
        if desc:
            instance_line += f"\n  - {desc}"

        # Find Historical Instances section and append
        if "## Historical Instances" in body:
            # Insert before the next ## section or at end
            sections = body.split("## Historical Instances")
            after = sections[1]
            # Find the next ## heading
            next_heading = after.find("\n## ")
            if next_heading != -1:
                after = after[:next_heading] + instance_line + "\n" + after[next_heading:]
            else:
                after = after.rstrip() + instance_line + "\n"
            body = sections[0] + "## Historical Instances" + after
        else:
            body += f"\n## Historical Instances{instance_line}\n"

    # Add new variant
    new_variant = data.get("new_variant")
    if new_variant and "## Variants" in body:
        body = body.replace("## Variants", f"## Variants\n- {new_variant}", 1)

    # Add new keywords
    new_kw = data.get("new_keywords", [])
    if new_kw:
        existing_kw = set(meta.keywords)
        for kw in new_kw:
            if str(kw) not in existing_kw:
                meta.keywords.append(str(kw))

    # Rewrite file
    full_content = serialize_frontmatter(meta, body)
    store.write(existing_path, full_content)

    return f"APPENDED: {existing_path} (+{len(new_kw)} keywords, instance: {hist.get('report_id', '?')})"


def record_progress(store: MemoryStore, report_id: str, report_path: Path, result: str) -> None:
    """Record processed report in progress file."""
    project = report_path.parent.parent.name
    line = f"- {report_id} ({project}) \u2192 {result}"
    store.append(PROGRESS_FILE, line + "\n", timestamp=False)


async def process_one(
    report_path: Path,
    batch_prompt: str,
    existing_notes: list[dict],
    store: MemoryStore,
    graph: KnowledgeGraph,
    dry_run: bool,
) -> str:
    """Process a single report through Sonnet.

    Sequential execution is intentional: each report's output updates
    existing_notes for dedup context in subsequent reports.
    """
    report_id = extract_report_id(report_path)

    log.info("Processing %s (%s)", report_id, report_path.parent.parent.name)
    report_content = report_path.read_text(encoding="utf-8")
    prompt = build_sonnet_prompt(batch_prompt, report_content, existing_notes)

    t0 = time.monotonic()
    data = await call_sonnet(prompt)
    elapsed = time.monotonic() - t0

    if data is None:
        # Retry once with explicit JSON-only instruction
        log.warning("%s: first attempt failed, retrying with JSON reminder", report_id)
        retry_prompt = prompt + (
            "\n\nIMPORTANT: You MUST respond with ONLY a JSON object. "
            "No prose, no analysis, no explanation. Output raw JSON starting with { and ending with }."
        )
        data = await call_sonnet(retry_prompt)
        elapsed = time.monotonic() - t0
        if data is None:
            result = "ERROR: Sonnet returned no parseable output (after retry)"
            log.error("%s: %s (%.1fs)", report_id, result, elapsed)
            if not dry_run:
                record_progress(store, report_id, report_path, result)
            return result

    action = data.get("action", "unknown")

    if dry_run:
        log.info("%s: %s (%.1fs)", report_id, action, elapsed)
        print(f"\n{'=' * 60}")
        print(f"Report: {report_id} ({report_path.parent.parent.name})")
        print(f"Action: {action} (took {elapsed:.1f}s)")
        print(json.dumps(data, indent=2))
        print(f"{'=' * 60}")
        # Track created notes for dedup even in dry-run
        if action == "create" and data.get("path"):
            existing_notes.append(
                {
                    "path": data["path"],
                    "keywords": data.get("keywords", []),
                    "tags": data.get("tags", []),
                    "importance": data.get("importance", 0.5),
                }
            )
        # Return with standard prefix for counter tracking
        if action == "create":
            return f"CREATED (dry-run): {data.get('path', '?')}"
        elif action == "append":
            return f"APPENDED (dry-run): {data.get('existing_path', '?')}"
        elif action == "skip":
            return f"SKIPPED (dry-run): {data.get('reason', '?')}"
        return f"UNKNOWN (dry-run): {action}"

    if action == "create":
        result = apply_create(data, store, graph)
    elif action == "append":
        result = apply_append(data, store, graph)
    elif action == "skip":
        reason = data.get("reason", "no reason")
        result = f"SKIPPED: {reason}"
    else:
        result = f"UNKNOWN action: {action}"
        log.warning("%s: raw Sonnet output: %s", report_id, json.dumps(data)[:500])

    log.info("%s: %s (%.1fs)", report_id, result, elapsed)
    record_progress(store, report_id, report_path, result)

    # Update existing_notes in-place for dedup on subsequent reports
    if action == "create" and data.get("path"):
        existing_notes.append(
            {
                "path": data["path"],
                "keywords": data.get("keywords", []),
                "tags": data.get("tags", []),
                "importance": data.get("importance", 0.5),
            }
        )

    return result


def _acquire_lock() -> int:
    """Acquire exclusive lockfile. Returns fd. Raises SystemExit if already running."""
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCKFILE), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        log.error("Another batch_import instance is already running (lockfile: %s)", LOCKFILE)
        sys.exit(1)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


def _release_lock(fd: int) -> None:
    """Release lockfile."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        LOCKFILE.unlink(missing_ok=True)
    except OSError:
        pass


async def main() -> None:
    parser = argparse.ArgumentParser(description="Batch import vulnerability reports")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--limit", type=int, default=0, help="Max reports to process")
    parser.add_argument("--severity", default="critical", help="Severity level (default: critical)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Exclusive lock — prevents multiple instances
    lock_fd = _acquire_lock()

    def _shutdown(signum, frame):
        """Kill active child process group on signal, then exit."""
        if _active_proc and _active_proc.returncode is None:
            _kill_proc(_active_proc)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        await _run(args, lock_fd)
    finally:
        _release_lock(lock_fd)


async def _run(args: argparse.Namespace, lock_fd: int) -> None:
    # Initialize store + graph
    store = MemoryStore(MEMORY_DIR)
    graph = KnowledgeGraph(MEMORY_DIR)

    # Force index sync so existing notes are visible
    store._index.ensure_index()

    # Load components
    batch_prompt = load_batch_prompt()
    existing_notes = load_existing_notes(store, graph)
    processed_ids = load_processed_ids(store)

    log.info("Loaded %d existing notes, %d processed IDs", len(existing_notes), len(processed_ids))

    # Collect reports
    reports = collect_reports(severity=args.severity, limit=args.limit)
    log.info("Found %d %s reports", len(reports), args.severity)

    # Filter already-processed
    todo = []
    for r in reports:
        rid = extract_report_id(r)
        if rid in processed_ids:
            log.debug("Skipping %s (already processed)", rid)
        else:
            todo.append(r)

    log.info("%d reports to process (%d already done)", len(todo), len(reports) - len(todo))

    if not todo:
        log.info("Nothing to process")
        return

    # Initialize progress file if needed
    if not (MEMORY_DIR / PROGRESS_FILE).exists():
        store.write(
            PROGRESS_FILE,
            "# Batch Processing Progress\n\nTrack processed reports to avoid reprocessing.\n\n## Processed Reports\n",
        )

    # Process sequentially — each report's output updates dedup context for the next
    t0 = time.monotonic()

    results = {"created": 0, "appended": 0, "skipped": 0, "error": 0}

    for i, report in enumerate(todo):
        result = await process_one(report, batch_prompt, existing_notes, store, graph, args.dry_run)
        if result.startswith("CREATED"):
            results["created"] += 1
        elif result.startswith("APPENDED"):
            results["appended"] += 1
        elif result.startswith("SKIPPED"):
            results["skipped"] += 1
        else:
            results["error"] += 1

        # Stats every 20 reports
        if (i + 1) % 20 == 0 and not args.dry_run:
            stats = graph.stats()
            log.info(
                "Progress: %d/%d | created=%d appended=%d skipped=%d errors=%d | graph: %d notes, %d relations",
                i + 1,
                len(todo),
                results["created"],
                results["appended"],
                results["skipped"],
                results["error"],
                stats.get("notes", 0),
                stats.get("relations", 0),
            )

    elapsed = time.monotonic() - t0
    log.info(
        "Done in %.1fs: created=%d appended=%d skipped=%d errors=%d",
        elapsed,
        results["created"],
        results["appended"],
        results["skipped"],
        results["error"],
    )

    # Final stats
    if not args.dry_run:
        stats = graph.stats()
        log.info(
            "Graph stats: %d notes, %d relations, %d tags",
            stats.get("notes", 0),
            stats.get("relations", 0),
            stats.get("unique_tags", 0),
        )

    graph.close()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
