#!/usr/bin/env python3
"""Sub-agent MCP server — provides orchestration tools to Claude sessions.

Spawned per Claude session by SessionManager.  All operations delegate to the
main process via bridge RPC.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure the project root is importable
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class SubAgentMCPServer(MCPServerBase):
    name = "subagent"
    version = "1.0.0"
    gateway_description = "Multi-agent orchestration (spawn/wait/status/send/stop/diff/merge/delete). action='help' for details."
    tools = [
        {
            "name": "subagent_spawn",
            "description": (
                "Spawn a sub-agent worker session to execute a task independently. "
                "Set worktree=true for an isolated git worktree. "
                "Paradigms: 'coder' (default, full access), 'reviewer' (read-only), "
                "'researcher' (read-only). Returns agent_id immediately."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task description for the worker to execute",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable name for the worker (used in branch names)",
                    },
                    "worktree": {
                        "type": "boolean",
                        "description": "Create an isolated git worktree for this worker",
                        "default": False,
                    },
                    "paradigm": {
                        "type": "string",
                        "description": "Worker role: 'coder', 'reviewer', 'researcher', or custom",
                        "default": "coder",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "subagent_wait",
            "description": (
                "BLOCKING: Wait until ALL specified sub-agents complete. "
                "Your session will be fully blocked until agents finish or timeout. "
                "Spawn all workers BEFORE calling wait. Returns results for all agents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of agent IDs to wait for",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 3600)",
                        "default": 3600,
                    },
                },
                "required": ["agent_ids"],
            },
        },
        {
            "name": "subagent_status",
            "description": (
                "Get sub-agent status. With agent_id: detailed info, diff summary, and cost "
                "(set include_result=true for full result). Without agent_id: list all sub-agents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to query. Omit to list all sub-agents.",
                    },
                    "include_result": {
                        "type": "boolean",
                        "description": "Include full result text (only with agent_id).",
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "subagent_send",
            "description": (
                "Send a follow-up prompt to a sub-agent. "
                "Works on running agents (queue append) and completed/stopped agents "
                "(automatically re-activates). Fails on failed/merged agents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to send to",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Follow-up prompt to send",
                    },
                },
                "required": ["agent_id", "prompt"],
            },
        },
        {
            "name": "subagent_stop",
            "description": "Stop a running sub-agent (interrupts current task, drains queue).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to stop",
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "subagent_diff",
            "description": (
                "Get the full git diff for a sub-agent's worktree "
                "(changes relative to the parent branch)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to get diff for",
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "subagent_merge",
            "description": (
                "Squash-merge a sub-agent's worktree into the parent branch. "
                "Changes are staged but NOT committed — review and commit manually. "
                "Cleans up worktree and session after merge."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to merge",
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "subagent_delete",
            "description": (
                "Delete a completed/stopped/failed/merged sub-agent. "
                "Destroys session, cleans up worktree if present, and removes the record. "
                "Cannot delete running agents — stop them first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to delete",
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "subagent_reclaim_respond",
            "description": (
                "Respond to a sub-agent slot reclamation request when another "
                "session needs your idle sub-agent's slot."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "The request_id from the reclamation request",
                    },
                    "approve": {
                        "type": "boolean",
                        "description": "True to approve deletion, False to deny",
                    },
                },
                "required": ["request_id", "approve"],
            },
        },
        {
            "name": "session_info",
            "description": (
                "Get metadata about your own session: session ID, status, runtime, "
                "prompt count, last cost, working directory, and context fields. "
                "Useful for self-awareness and decision-making."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "subagent_spawn": self._handle_spawn,
            "subagent_wait": self._handle_wait,
            "subagent_status": self._handle_status,
            "subagent_send": self._handle_send,
            "subagent_stop": self._handle_stop,
            "subagent_diff": self._handle_diff,
            "subagent_merge": self._handle_merge,
            "subagent_delete": self._handle_delete,
            "subagent_reclaim_respond": self._handle_reclaim_respond,
            "session_info": self._handle_session_info,
        }

    def _bridge_call(self, method: str, extra_params: dict, timeout: float = 60) -> dict:
        """Call bridge with session_id injected."""
        if not self.bridge:
            return {"error": "Bridge not available"}

        params = {"session_id": self.session_id}
        params.update(extra_params)

        try:
            return self.bridge.call(method, params, timeout=timeout)
        except TimeoutError:
            return {"error": "Request timed out"}
        except (ConnectionError, RuntimeError) as e:
            return {"error": f"Bridge error: {e}"}

    def _format_result(self, result: dict) -> str:
        """Format bridge result as human-readable string."""
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)

    # -- handlers ------------------------------------------------------------

    def _handle_spawn(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_spawn",
            {
                "task": args.get("task", ""),
                "name": args.get("name", "worker"),
                "worktree": args.get("worktree", False),
                "paradigm": args.get("paradigm", "coder"),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"

        agent_id = result.get("agent_id", "")
        lines = [
            f"Spawned sub-agent '{result.get('name', '')}'",
            f"agent_id: {agent_id}",
            f"Paradigm: {result.get('paradigm', '')}",
            f"Status: {result.get('status', '')}",
        ]
        branch = result.get("worktree_branch")
        if branch:
            lines.append(f"Worktree branch: {branch}")
        lines.append("")
        lines.append("Use this agent_id for subagent_wait, subagent_status, etc.")
        return "\n".join(lines)

    def _handle_wait(self, args: dict) -> str:
        timeout = args.get("timeout", 3600)
        result = self._bridge_call(
            "subagent_wait",
            {
                "agent_ids": args.get("agent_ids", []),
                "timeout": timeout,
            },
            # Bridge timeout slightly longer so PendingStore times out first
            timeout=timeout + 30,
        )
        if "error" in result:
            return f"Error: {result['error']}"

        results = result.get("results", {})
        all_ok = result.get("all_completed", False)

        lines = [f"All completed: {all_ok}"]
        for aid, r in results.items():
            status = r.get("status", "unknown")
            cost = r.get("cost_usd")
            cost_str = f" (${cost:.4f})" if cost else ""
            summary = r.get("result", "")
            lines.append(f"\n[{aid}] {status}{cost_str}")
            if summary:
                lines.append(f"  {summary}")
            if r.get("error"):
                lines.append(f"  Error: {r['error']}")
        return "\n".join(lines)

    def _handle_status(self, args: dict) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return self._handle_list(args)
        result = self._bridge_call(
            "subagent_status",
            {
                "agent_id": agent_id,
                "include_result": args.get("include_result", False),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"

        lines = [
            f"Agent: {result.get('name', '')} (agent_id: {result.get('agent_id', '')})",
            f"Status: {result.get('status', '')}",
            f"Paradigm: {result.get('paradigm', '')}",
            f"Created: {result.get('created_at', '')}",
        ]
        if result.get("completed_at"):
            lines.append(f"Completed: {result['completed_at']}")
        if result.get("cost_usd") is not None:
            lines.append(f"Cost: ${result['cost_usd']:.4f}")
        if result.get("worktree_branch"):
            lines.append(f"Branch: {result['worktree_branch']}")
        if result.get("diff_stat"):
            lines.append(f"Changes:\n{result['diff_stat']}")
        if result.get("result"):
            lines.append(f"\nResult:\n{result['result']}")
        if result.get("error"):
            lines.append(f"\nError:\n{result['error']}")
        return "\n".join(lines)

    def _handle_send(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_send",
            {
                "agent_id": args.get("agent_id", ""),
                "prompt": args.get("prompt", ""),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"
        pos = result.get("queue_position", 0)
        return f"Prompt sent. Queue position: {pos}"

    def _handle_stop(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_stop",
            {
                "agent_id": args.get("agent_id", ""),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"
        return "Agent stopped."

    def _handle_list(self, args: dict) -> str:
        result = self._bridge_call("subagent_list", {})
        if "error" in result:
            return f"Error: {result['error']}"

        agents = result.get("agents", [])
        if not agents:
            return "No sub-agents."

        lines = [f"{len(agents)} sub-agent(s):"]
        for a in agents:
            cost = a.get("cost_usd")
            cost_str = f" ${cost:.4f}" if cost else ""
            branch = a.get("worktree_branch")
            branch_str = f" [{branch}]" if branch else ""
            lines.append(
                f"- {a.get('name', '')} (agent_id: {a.get('agent_id', '')}) "
                f"[{a.get('status', '')}] {a.get('paradigm', '')}"
                f"{branch_str}{cost_str}"
            )
        return "\n".join(lines)

    def _handle_diff(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_diff",
            {
                "agent_id": args.get("agent_id", ""),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"

        stat = result.get("stat", "")
        diff = result.get("diff", "")
        truncated = result.get("truncated", False)

        lines = []
        if stat:
            lines.append(f"Summary:\n{stat}")
        if diff:
            lines.append(f"\nDiff:\n{diff}")
        if truncated:
            lines.append("\n[Diff truncated. Use git diff directly for full output.]")
        return "\n".join(lines) if lines else "No changes."

    def _handle_merge(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_merge",
            {
                "agent_id": args.get("agent_id", ""),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"

        staged = result.get("staged_files", [])
        lines = [
            "Merge successful!",
            f"Staged {len(staged)} file(s):",
        ]
        for f in staged[:20]:
            lines.append(f"  - {f}")
        if len(staged) > 20:
            lines.append(f"  ... and {len(staged) - 20} more")
        lines.append("")
        lines.append(result.get("note", "Review and commit manually."))
        return "\n".join(lines)

    def _handle_delete(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_delete",
            {
                "agent_id": args.get("agent_id", ""),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"
        return "Agent deleted."

    def _handle_reclaim_respond(self, args: dict) -> str:
        result = self._bridge_call(
            "subagent_reclaim_respond",
            {
                "request_id": args.get("request_id", ""),
                "approve": args.get("approve", False),
            },
        )
        if "error" in result:
            return f"Error: {result['error']}"
        approved = result.get("approved", False)
        return f"Reclamation {'approved' if approved else 'denied'}."

    # Context keys safe to expose (session-relevant, no routing data from other extensions)
    _CONTEXT_ALLOWLIST = {
        "subagent_worker",
        "subagent_parent_id",
        "subagent_paradigm",
        "subagent_task",
        "subagent_worktree_branch",
        "subagent_auto_cleanup",
        "heartbeat_auto_cleanup",
        "cron_auto_cleanup",
    }

    def _handle_session_info(self, args: dict) -> str:
        state = self.session_context()

        # Compute runtime
        runtime_seconds = None
        created_at = state.get("created_at", "")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at)
                runtime_seconds = int((datetime.now(UTC) - created).total_seconds())
            except (ValueError, TypeError):
                pass

        # Extract last prompt cost from metadata
        meta = state.get("last_result_metadata", {})

        # Filter context to allowlisted keys only
        raw_ctx = state.get("context", {})
        filtered_ctx = {k: v for k, v in raw_ctx.items() if k in self._CONTEXT_ALLOWLIST}

        info = {
            "session_id": state.get("id") or self.session_id,
            "name": state.get("name"),
            "status": state.get("status"),
            "user_id": state.get("user_id") or self.session_user_id,
            "working_dir": state.get("working_dir"),
            "created_at": created_at or None,
            "last_active_at": state.get("last_active_at"),
            "runtime_seconds": runtime_seconds,
            "prompt_count": state.get("prompt_count", 0),
            "last_cost_usd": meta.get("total_cost_usd"),
            "last_model": meta.get("model"),
            "claude_session_id": state.get("claude_session_id"),
            "context": filtered_ctx,
        }
        return json.dumps(info, indent=2)


if __name__ == "__main__":
    SubAgentMCPServer().run()
