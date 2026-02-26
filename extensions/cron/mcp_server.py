#!/usr/bin/env python3
"""Minimal MCP stdio server for cron job management.

Spawned by Claude Code per session.  Reads session context from env vars
and communicates with the shared job store via file locking.

No external dependencies beyond the Python stdlib + extensions.cron.store.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is importable (mcp_server.py lives in extensions/cron/)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from extensions.cron.store import JobStore, parse_relative_time  # noqa: E402

# ---------------------------------------------------------------------------
# Environment (injected per-session by SessionManager)
# ---------------------------------------------------------------------------

STORE_PATH = os.environ.get("CRON_STORE_PATH", "")
SESSION_ID = os.environ.get("CLAUDE_EXT_SESSION_ID", "")
STATE_DIR = os.environ.get("CLAUDE_EXT_STATE_DIR", "")

# ---------------------------------------------------------------------------
# Session context (read from state.json)
# ---------------------------------------------------------------------------

def _load_session_context() -> dict:
    """Read the current session's state.json for user_id, context, working_dir."""
    if not STATE_DIR:
        return {}
    state_file = Path(STATE_DIR) / "state.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "cron_create",
        "description": (
            "Schedule a task for Claude to execute later. "
            "Use 'cron_expr' for recurring schedules (e.g. '0 8 * * *' for daily at 8am), "
            "or 'run_at' for one-time delays (e.g. '+20m', '+1h', '+2h30m'). "
            "Set session_strategy to 'reuse' to continue in the current session context, "
            "or 'new' (default) for an independent session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable name for the job",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to send to Claude when the job triggers",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression for recurring jobs (e.g. '0 8 * * *', '*/30 * * * *')",
                },
                "run_at": {
                    "type": "string",
                    "description": "Relative time for one-time jobs (e.g. '+20m', '+1h', '+2d')",
                },
                "session_strategy": {
                    "type": "string",
                    "enum": ["new", "reuse"],
                    "description": "Session strategy: 'new' creates a fresh session; 'reuse' continues in the current session with full context",
                    "default": "new",
                },
            },
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "cron_list",
        "description": "List all scheduled cron jobs for the current user.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "cron_delete",
        "description": "Delete a scheduled cron job by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to delete",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "cron_status",
        "description": "Get detailed status of a specific cron job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to query",
                },
            },
            "required": ["job_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _get_store() -> JobStore:
    if not STORE_PATH:
        raise RuntimeError("CRON_STORE_PATH not set")
    return JobStore(Path(STORE_PATH))


def handle_cron_create(args: dict) -> str:
    ctx = _load_session_context()
    user_id = ctx.get("user_id", "unknown")
    working_dir = ctx.get("working_dir", os.getcwd())
    notify_context = ctx.get("context", {})  # includes chat_id etc.

    name = args["name"]
    prompt = args["prompt"]
    cron_expr = args.get("cron_expr")
    run_at_expr = args.get("run_at")
    strategy = args.get("session_strategy", "new")

    # Validate: exactly one trigger
    if not cron_expr and not run_at_expr:
        return "Error: Either 'cron_expr' or 'run_at' must be provided."
    if cron_expr and run_at_expr:
        return "Error: Provide only one of 'cron_expr' or 'run_at', not both."

    # Parse run_at
    run_at_iso = None
    if run_at_expr:
        dt = parse_relative_time(run_at_expr)
        if dt is None:
            return f"Error: Cannot parse run_at expression '{run_at_expr}'. Use format like '+20m', '+1h', '+2d'."
        run_at_iso = dt.isoformat()

    # For reuse strategy, capture current session's Claude session ID
    session_id = None
    claude_session_id = None
    if strategy == "reuse":
        session_id = SESSION_ID
        claude_session_id = ctx.get("claude_session_id")

    store = _get_store()
    job = store.create_job(
        name=name,
        prompt=prompt,
        working_dir=working_dir,
        user_id=user_id,
        cron_expr=cron_expr,
        run_at=run_at_iso,
        session_strategy=strategy,
        session_id=session_id,
        claude_session_id=claude_session_id,
        notify_context=notify_context,
        created_by=SESSION_ID,
    )
    store.add_job(job)

    trigger = cron_expr if cron_expr else f"at {run_at_iso}"
    return (
        f"Scheduled job '{name}' (ID: {job.id[:8]})\n"
        f"Trigger: {trigger}\n"
        f"Strategy: {strategy}\n"
        f"Next run: {job.next_run}"
    )


def handle_cron_list(args: dict) -> str:
    ctx = _load_session_context()
    user_id = ctx.get("user_id", "unknown")

    store = _get_store()
    jobs = store.list_jobs(user_id=user_id)

    if not jobs:
        return "No scheduled jobs."

    lines = []
    for j in jobs:
        status = "enabled" if j.enabled else "paused"
        trigger = j.cron_expr or f"once at {j.run_at}"
        lines.append(
            f"- {j.name} (ID: {j.id[:8]}) [{status}]\n"
            f"  Trigger: {trigger} | Strategy: {j.session_strategy}\n"
            f"  Next: {j.next_run or 'N/A'} | Last: {j.last_run or 'never'}"
        )

    return f"{len(jobs)} job(s):\n" + "\n".join(lines)


def handle_cron_delete(args: dict) -> str:
    job_id = args["job_id"]
    store = _get_store()

    # Support short ID prefix
    jobs = store.list_jobs()
    match = [j for j in jobs if j.id.startswith(job_id)]
    if not match:
        return f"Error: No job found with ID prefix '{job_id}'."
    if len(match) > 1:
        return f"Error: Ambiguous ID prefix '{job_id}' matches {len(match)} jobs."

    store.delete_job(match[0].id)
    return f"Deleted job '{match[0].name}' ({match[0].id[:8]})."


def handle_cron_status(args: dict) -> str:
    job_id = args["job_id"]
    store = _get_store()

    jobs = store.list_jobs()
    match = [j for j in jobs if j.id.startswith(job_id)]
    if not match:
        return f"Error: No job found with ID prefix '{job_id}'."

    j = match[0]
    return (
        f"Job: {j.name}\n"
        f"ID: {j.id}\n"
        f"Prompt: {j.prompt[:200]}\n"
        f"Trigger: {j.cron_expr or f'once at {j.run_at}'}\n"
        f"Strategy: {j.session_strategy}\n"
        f"Session ID: {j.session_id or 'N/A'}\n"
        f"Working dir: {j.working_dir}\n"
        f"Enabled: {j.enabled}\n"
        f"Created: {j.created_at}\n"
        f"Last run: {j.last_run or 'never'}\n"
        f"Next run: {j.next_run or 'N/A'}"
    )


HANDLERS = {
    "cron_create": handle_cron_create,
    "cron_list": handle_cron_list,
    "cron_delete": handle_cron_delete,
    "cron_status": handle_cron_status,
}

# ---------------------------------------------------------------------------
# MCP JSON-RPC protocol handler (stdio)
# ---------------------------------------------------------------------------

def write_msg(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle_message(msg: dict) -> dict | None:
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cron", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        try:
            result_text = handler(arguments)
        except Exception as e:
            result_text = f"Error: {e}"

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": result_text}],
            },
        }

    # Unknown method
    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main() -> None:
    """Read JSON-RPC messages from stdin, respond on stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_message(msg)
        if response is not None:
            write_msg(response)


if __name__ == "__main__":
    main()
