#!/usr/bin/env python3
"""Cron MCP server — manages scheduled jobs via Claude tool calls.

Spawned by Claude Code per session.  Inherits MCPServerBase for protocol
handling; only business logic lives here.
"""

import os
import sys
from pathlib import Path

# Ensure the project root is importable (mcp_server.py lives in extensions/cron/)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402
from extensions.cron.store import JobStore, parse_relative_time  # noqa: E402


class CronMCPServer(MCPServerBase):
    name = "cron"
    tools = [
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
            "description": (
                "Get cron job status. "
                "With job_id: detailed info for one job. "
                "Without: list all jobs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID to query. Omit to list all jobs.",
                    },
                },
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "cron_create": self._handle_create,
            "cron_delete": self._handle_delete,
            "cron_status": self._handle_status,
        }

    def _get_store(self) -> JobStore:
        store_path = os.environ.get("CRON_STORE_PATH", "")
        if not store_path:
            raise RuntimeError("CRON_STORE_PATH not set")
        return JobStore(Path(store_path))

    # -- handlers -----------------------------------------------------------

    def _handle_create(self, args: dict) -> str:
        ctx = self.session_context()
        user_id = ctx.get("user_id", "unknown")
        working_dir = ctx.get("working_dir", os.getcwd())
        notify_context = ctx.get("context", {})

        name = args["name"]
        prompt = args["prompt"]
        cron_expr = args.get("cron_expr")
        run_at_expr = args.get("run_at")
        strategy = args.get("session_strategy", "new")

        if not cron_expr and not run_at_expr:
            return "Error: Either 'cron_expr' or 'run_at' must be provided."
        if cron_expr and run_at_expr:
            return "Error: Provide only one of 'cron_expr' or 'run_at', not both."

        run_at_iso = None
        if run_at_expr:
            dt = parse_relative_time(run_at_expr)
            if dt is None:
                return f"Error: Cannot parse run_at expression '{run_at_expr}'. Use format like '+20m', '+1h', '+2d'."
            run_at_iso = dt.isoformat()

        session_id = None
        claude_session_id = None
        if strategy == "reuse":
            session_id = self.session_id
            claude_session_id = ctx.get("claude_session_id")

        store = self._get_store()
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
            created_by=self.session_id,
        )
        store.add_job(job)

        trigger = cron_expr if cron_expr else f"at {run_at_iso}"
        return (
            f"Scheduled job '{name}' (ID: {job.id[:8]})\n"
            f"Trigger: {trigger}\n"
            f"Strategy: {strategy}\n"
            f"Next run: {job.next_run}"
        )

    def _handle_list(self, args: dict) -> str:
        ctx = self.session_context()
        user_id = ctx.get("user_id", "unknown")

        store = self._get_store()
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

    def _handle_delete(self, args: dict) -> str:
        job_id = args["job_id"]
        store = self._get_store()

        jobs = store.list_jobs()
        match = [j for j in jobs if j.id.startswith(job_id)]
        if not match:
            return f"Error: No job found with ID prefix '{job_id}'."
        if len(match) > 1:
            return f"Error: Ambiguous ID prefix '{job_id}' matches {len(match)} jobs."

        store.delete_job(match[0].id)
        return f"Deleted job '{match[0].name}' ({match[0].id[:8]})."

    def _handle_status(self, args: dict) -> str:
        job_id = args.get("job_id")
        if not job_id:
            return self._handle_list(args)
        store = self._get_store()

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


if __name__ == "__main__":
    CronMCPServer().run()
