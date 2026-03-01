"""Sub-agent extension — multi-agent orchestration for Claude Code sessions.

Enables a PM session to spawn independent worker sessions (each in its own
tmux + optional git worktree), wait for completion, review diffs, and merge
results back.

Data flow::

    PM Claude → MCP tool → bridge.call("subagent_*") → BridgeServer handler
      → SessionManager (create/send/stop/destroy) + SubAgentStore + PendingStore
      → Worker sessions (independent tmux, independent worktree, parallel)
      → Delivery callback → PendingStore.resolve() → PM unblocks
"""

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core.extension import Extension
from core.session import SessionOverrides, SessionStatus
from extensions.subagent.store import MAX_RESULT_LENGTH, SubAgent, SubAgentStore
from extensions.subagent.worktree import (
    cleanup_worktree,
    create_worktree,
    get_worktree_diff,
    get_worktree_diff_stat_only,
    is_git_repo,
    squash_merge,
)

log = logging.getLogger(__name__)

# Default cleanup delay (seconds) after worker completion before session destroy.
_CLEANUP_DELAY = 120.0


def _check_all_completed(results: dict) -> bool:
    """Check if all agents in results dict are successfully completed.

    Returns False if there are no results, any errors, or any non-completed status.
    Avoids vacuous truth (all() on empty iterable returns True).
    """
    if not results:
        return False
    success_statuses = ("completed", "merged")
    has_errors = any("error" in r for r in results.values())
    if has_errors:
        return False
    return all(r.get("status") in success_statuses for r in results.values())


_SYSTEM_PROMPT = """\
You have sub-agent orchestration tools available. Use them to delegate tasks \
to independent worker sessions that run in parallel.

Tools: subagent_spawn, subagent_wait, subagent_status, \
subagent_send, subagent_stop, subagent_diff, subagent_merge.

Workflow: spawn workers → wait for completion → review diffs → merge → commit.

IMPORTANT: subagent_wait is BLOCKING — your session will be fully blocked \
until all specified agents complete or timeout. Spawn all workers before calling wait. \
You can also skip wait and poll with subagent_status instead."""


@dataclass
class Paradigm:
    """Worker role definition."""

    name: str
    system_prompt: str
    disallowed_tools: list[str] = field(default_factory=list)
    auto_cleanup: bool = True
    exclude_mcp_servers: set[str] = field(default_factory=set)


# Built-in paradigms
_BUILTIN_PARADIGMS = {
    "coder": Paradigm(
        name="coder",
        system_prompt=(
            "You are a coding worker agent. Execute the assigned task thoroughly.\n"
            "- Commit your changes frequently with clear messages.\n"
            "- When done, provide a concise summary of what you changed and why.\n"
            "- Do NOT interact with the user — focus solely on the task."
        ),
        disallowed_tools=[],
        auto_cleanup=True,
    ),
    "reviewer": Paradigm(
        name="reviewer",
        system_prompt=(
            "You are a code review / audit agent. Analyze the provided code or plan.\n"
            "- Provide structured feedback: issues, suggestions, and approval status.\n"
            "- Do NOT modify any files — your role is read-only analysis.\n"
            "- Be thorough but concise."
        ),
        disallowed_tools=["Write", "Edit", "NotebookEdit"],
        auto_cleanup=False,
        exclude_mcp_servers={"vault", "heartbeat", "cron", "ask_user"},
    ),
    "researcher": Paradigm(
        name="researcher",
        system_prompt=(
            "You are a research agent. Investigate and analyze the given topic.\n"
            "- Read files, search the codebase, and gather information.\n"
            "- Do NOT modify any files — your role is read-only research.\n"
            "- Provide a comprehensive summary of findings."
        ),
        disallowed_tools=["Write", "Edit", "NotebookEdit"],
        auto_cleanup=False,
        exclude_mcp_servers={"vault", "heartbeat", "cron", "ask_user"},
    ),
}


class ExtensionImpl(Extension):
    name = "subagent"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._max_subagents = config.get("max_subagents_per_session", 5)
        self._default_paradigm = config.get("default_paradigm", "coder")
        self._cleanup_delay = config.get("cleanup_delay", _CLEANUP_DELAY)
        self._custom_paradigms: dict[str, dict] = config.get("paradigms", {})

        self._store: SubAgentStore | None = None
        self._worktree_base: str = ""
        self._paradigms: dict[str, Paradigm] = {}

        # agent_id → pending_key for active waits.
        # Reads and writes are safe under asyncio cooperative multitasking
        # (no preemptive thread switches between yield points).
        self._wait_pendings: dict[str, str] = {}

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        # 1. Store init
        self._store = SubAgentStore(Path(self.sm.base_dir) / "subagent")

        # 2. Worktree base dir
        self._worktree_base = str(Path(self.sm.base_dir) / "worktrees")

        # 3. Load paradigms (built-in + config custom)
        self._load_paradigms()

        # 4. Service registry
        self.engine.services["subagent"] = self

        # 5. Register MCP server
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        self.sm.register_mcp_server(
            "subagent",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {},
            },
            tools=[
                {
                    "name": "subagent_spawn",
                    "description": "Spawn a sub-agent worker session",
                },
                {
                    "name": "subagent_wait",
                    "description": "Block until specified sub-agents complete",
                },
                {
                    "name": "subagent_status",
                    "description": "Get sub-agent status, or list all if no agent_id",
                },
                {
                    "name": "subagent_send",
                    "description": "Send follow-up prompt to a sub-agent",
                },
                {
                    "name": "subagent_stop",
                    "description": "Stop a running sub-agent",
                },
                {
                    "name": "subagent_diff",
                    "description": "Get worktree diff for a sub-agent",
                },
                {
                    "name": "subagent_merge",
                    "description": "Squash-merge sub-agent worktree into parent branch",
                },
            ],
        )

        # 6. Bridge handler
        if self.engine.bridge:
            self.engine.bridge.add_handler(self._bridge_handler)

        # 7. System prompt
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="subagent")

        # 8. Delivery callback
        self.sm.add_delivery_callback(self._on_delivery)

        # 9. Session customizer (prevent worker recursion + inject role)
        self.sm.add_session_customizer(self._customize_session)

        # 10. Recovery
        await self._recover()

        log.info(
            "Subagent extension started. paradigms=%s, max_per_session=%d",
            list(self._paradigms.keys()),
            self._max_subagents,
        )

    async def stop(self) -> None:
        # Cancel any active wait pendings
        for _agent_id, pending_key in list(self._wait_pendings.items()):
            self.engine.pending.resolve(pending_key, {"status": "cancelled"})
        self._wait_pendings.clear()

        self.engine.services.pop("subagent", None)
        log.info("Subagent extension stopped.")

    async def health_check(self) -> dict:
        if self._store is None:
            return {"status": "error", "detail": "SubAgentStore not initialized"}
        agents = self._store.list_agents()
        running = [a for a in agents if a.status == "running"]
        orphan_worktrees = [
            a
            for a in agents
            if a.worktree_enabled
            and a.worktree_path
            and a.status in ("failed", "stopped")
            and Path(a.worktree_path).exists()
        ]
        return {
            "status": "ok",
            "total_agents": len(agents),
            "running": len(running),
            "orphan_worktrees": len(orphan_worktrees),
        }

    # -- paradigms -----------------------------------------------------------

    def _load_paradigms(self) -> None:
        """Load built-in paradigms and merge in config-defined ones."""
        self._paradigms = dict(_BUILTIN_PARADIGMS)
        for name, pdef in self._custom_paradigms.items():
            self._paradigms[name] = Paradigm(
                name=name,
                system_prompt=pdef.get("system_prompt", ""),
                disallowed_tools=pdef.get("disallowed_tools", []),
                auto_cleanup=pdef.get("auto_cleanup", True),
                exclude_mcp_servers=set(pdef.get("exclude_mcp_servers", [])),
            )

    def _get_paradigm(self, name: str) -> Paradigm:
        return self._paradigms.get(
            name, self._paradigms.get(self._default_paradigm, _BUILTIN_PARADIGMS["coder"])
        )

    # -- session customizer --------------------------------------------------

    def _customize_session(self, session):
        """Prevent workers from spawning sub-agents + inject role prompt."""
        if not session.context.get("subagent_worker"):
            return None  # not a worker, don't interfere

        paradigm_name = session.context.get("subagent_paradigm", "coder")
        paradigm = self._get_paradigm(paradigm_name)
        task = session.context.get("subagent_task", "")

        worker_prompt_parts = [paradigm.system_prompt]

        # Explicitly tell the worker its working directory and git branch
        worker_prompt_parts.append(
            f"\n## Working Directory\n"
            f"Your working directory is: {session.working_dir}\n"
            f"ALL file operations (Read, Write, Edit, Bash) MUST use this directory.\n"
            f"Do NOT navigate to or modify files outside this directory."
        )
        wt_branch = session.context.get("subagent_worktree_branch")
        if wt_branch:
            worker_prompt_parts.append(
                f"You are on git branch: {wt_branch}\n"
                f"Commit your changes to THIS branch. Do NOT switch branches."
            )

        if task:
            worker_prompt_parts.append(f"\n## Assigned Task\n{task}")

        exclude = {"subagent"}  # workers cannot spawn sub-agents
        exclude |= paradigm.exclude_mcp_servers  # paradigm-specific exclusions

        overrides = SessionOverrides(
            exclude_mcp_servers=exclude,
            extra_system_prompt=worker_prompt_parts,
        )
        if paradigm.disallowed_tools:
            overrides.extra_disallowed_tools = list(paradigm.disallowed_tools)
        return overrides

    # -- delivery callback ---------------------------------------------------

    async def _on_delivery(self, session_id: str, result_text: str, metadata: dict) -> None:
        """Track worker completion, resolve pending waits, notify parent."""
        session = self.sm.sessions.get(session_id)
        if not session or not session.context.get("subagent_worker"):
            return

        if not metadata.get("is_final"):
            return

        # 1. Update store record
        is_error = metadata.get("is_error", False)
        is_stopped = metadata.get("is_stopped", False)

        if is_error:
            new_status = "failed"
        elif is_stopped:
            new_status = "stopped"
        else:
            new_status = "completed"

        summary = result_text[:MAX_RESULT_LENGTH] if result_text else None
        cost = metadata.get("total_cost_usd")

        self._store.update_agent(
            session_id,
            status=new_status,
            completed_at=datetime.now(UTC).isoformat(),
            result_summary=summary,
            cost_usd=cost,
            error=result_text[:500] if is_error else None,
        )

        if self.engine.events:
            self.engine.events.log(
                "subagent.completed",
                session_id=session_id,
                detail={
                    "status": new_status,
                    "cost_usd": cost,
                    "parent": session.context.get("subagent_parent_id", "")[:8],
                },
            )

        # 2. Resolve pending wait if any
        pending_key = self._wait_pendings.pop(session_id, None)
        if pending_key:
            self.engine.pending.resolve(
                pending_key,
                {
                    "status": new_status,
                    "result": summary,
                    "cost_usd": cost,
                },
            )

        # 3. Notify parent session frontend (informational)
        parent_id = session.context.get("subagent_parent_id")
        if parent_id and parent_id in self.sm.sessions:
            agent = self._store.get_agent(session_id)
            agent_name = agent.name if agent else session_id[:8]
            notify_text = f"[Sub-agent '{agent_name}' {new_status}]"
            if cost:
                notify_text += f" (${cost:.4f})"
            await self.sm.deliver(
                parent_id,
                notify_text,
                {
                    "is_subagent_notification": True,
                    "subagent_id": session_id,
                    "subagent_status": new_status,
                },
            )

        # 4. Auto-cleanup session (delayed, after resolve)
        paradigm_name = session.context.get("subagent_paradigm", "coder")
        paradigm = self._get_paradigm(paradigm_name)
        should_cleanup = session.context.get("subagent_auto_cleanup", paradigm.auto_cleanup)
        if should_cleanup and new_status in ("completed", "stopped"):
            asyncio.create_task(self._delayed_cleanup(session_id))

    async def _delayed_cleanup(self, session_id: str) -> None:
        """Destroy a worker session after a brief delay (preserves worktree)."""
        try:
            await asyncio.sleep(self._cleanup_delay)
            session = self.sm.sessions.get(session_id)
            if session and session.status in (SessionStatus.IDLE, SessionStatus.STOPPED):
                await self.sm.destroy_session(session_id)
                log.info("Auto-cleaned sub-agent session %s", session_id[:8])
        except Exception:
            log.exception("Failed to auto-cleanup sub-agent session %s", session_id[:8])

    # -- recovery ------------------------------------------------------------

    async def _recover(self) -> None:
        """On startup, mark running agents with dead sessions as failed."""
        agents = self._store.list_agents()
        for agent in agents:
            if agent.status in ("pending", "running"):
                session = self.sm.sessions.get(agent.id)
                if not session or session.status == SessionStatus.DEAD:
                    self._store.update_agent(
                        agent.id,
                        status="failed",
                        error="Session lost during restart",
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                    log.warning(
                        "Recovery: marked sub-agent %s (%s) as failed",
                        agent.name,
                        agent.id[:8],
                    )

    # -- bridge handler ------------------------------------------------------

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        """Dispatch sub-agent RPC methods."""
        handlers = {
            "subagent_spawn": self._handle_spawn,
            "subagent_wait": self._handle_wait,
            "subagent_status": self._handle_status,
            "subagent_send": self._handle_send,
            "subagent_stop": self._handle_stop,
            "subagent_list": self._handle_list,
            "subagent_diff": self._handle_diff,
            "subagent_merge": self._handle_merge,
        }
        handler = handlers.get(method)
        if handler is None:
            return None  # not ours
        try:
            return await handler(params)
        except Exception as e:
            log.exception("Error in subagent handler %s", method)
            return {"error": str(e)}

    # -- spawn ---------------------------------------------------------------

    async def _handle_spawn(self, params: dict) -> dict:
        parent_session_id = params.get("session_id", "")
        task = params.get("task", "")
        name = params.get("name", "worker")
        use_worktree = params.get("worktree", False)
        paradigm_name = params.get("paradigm", self._default_paradigm)

        if not task:
            return {"error": "task is required"}

        parent_session = self.sm.sessions.get(parent_session_id)
        if not parent_session:
            return {"error": f"Parent session {parent_session_id[:8]} not found"}

        user_id = parent_session.user_id
        working_dir = parent_session.working_dir

        # Check per-session sub-agent limit
        existing = self._store.list_agents(parent_session_id=parent_session_id)
        active = [a for a in existing if a.status in ("pending", "running")]
        if len(active) >= self._max_subagents:
            return {
                "error": f"Max active sub-agents ({self._max_subagents}) reached. "
                f"Wait for existing agents to complete or stop them."
            }

        # Check user session slots; try to reclaim if full
        user_sessions = self.sm.get_sessions_for_user(user_id)
        if len(user_sessions) >= self.sm.max_sessions_per_user:
            reclaimed = await self._reclaim_session(user_id, parent_session_id)
            if not reclaimed:
                return {
                    "error": f"No session slots available (limit: {self.sm.max_sessions_per_user}). "
                    "Stop or destroy existing sessions first."
                }

        # Worktree setup
        worktree_path = None
        worktree_branch = None
        parent_branch = None
        short_id = uuid.uuid4().hex[:8]

        if use_worktree:
            if not await is_git_repo(working_dir):
                return {"error": f"Working directory is not a git repo: {working_dir}"}
            try:
                worktree_path, worktree_branch, parent_branch = await create_worktree(
                    repo_dir=working_dir,
                    base_dir=self._worktree_base,
                    name=name,
                    short_id=short_id,
                )
                working_dir = worktree_path
            except RuntimeError as e:
                return {"error": f"Worktree creation failed: {e}"}

        # Build context
        context = dict(parent_session.context)  # inherit routing (chat_id etc.)
        context.update(
            {
                "subagent_worker": True,
                "subagent_parent_id": parent_session_id,
                "subagent_task": task[:500],
                "subagent_paradigm": paradigm_name,
                "subagent_worktree_branch": worktree_branch,  # None if no worktree
                "subagent_auto_cleanup": params.get(
                    "auto_cleanup",
                    self._get_paradigm(paradigm_name).auto_cleanup,
                ),
            }
        )

        # Create session
        try:
            session = await self.sm.create_session(
                name=f"sub-{name[:20]}",
                user_id=user_id,
                working_dir=working_dir,
                context=context,
            )
        except RuntimeError as e:
            # Cleanup worktree on session creation failure
            if worktree_path and worktree_branch:
                await cleanup_worktree(parent_session.working_dir, worktree_path, worktree_branch)
            return {"error": f"Session creation failed: {e}"}

        # Store agent record
        agent = SubAgent(
            id=session.id,
            parent_session_id=parent_session_id,
            name=name,
            task=task,
            paradigm=paradigm_name,
            user_id=user_id,
            working_dir=working_dir,
            worktree_enabled=use_worktree,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            parent_branch=parent_branch,
            status="running",
            created_at=datetime.now(UTC).isoformat(),
        )
        self._store.add_agent(agent)

        # Send initial prompt
        prompt = f"## Task\n{task}\n\nExecute this task now. Summarize results when done."
        await self.sm.send_prompt(session.id, prompt)

        if self.engine.events:
            self.engine.events.log(
                "subagent.spawned",
                session_id=session.id,
                detail={
                    "parent": parent_session_id[:8],
                    "name": name,
                    "paradigm": paradigm_name,
                    "worktree": use_worktree,
                },
            )

        return {
            "agent_id": session.id,
            "name": name,
            "paradigm": paradigm_name,
            "worktree_branch": worktree_branch,
            "status": "running",
        }

    # -- wait ----------------------------------------------------------------

    async def _handle_wait(self, params: dict) -> dict:
        agent_ids = params.get("agent_ids", [])
        timeout = params.get("timeout", 3600)

        if not agent_ids:
            return {"error": "agent_ids is required"}

        results = {}

        # Collect already-completed agents
        pending_ids = []
        for aid in agent_ids:
            agent = self._store.get_agent(aid)
            if not agent:
                results[aid] = {"error": "not found"}
            elif agent.status in ("completed", "failed", "stopped", "merged"):
                results[aid] = {
                    "status": agent.status,
                    "result": agent.result_summary,
                    "cost_usd": agent.cost_usd,
                }
            else:
                pending_ids.append(aid)

        if not pending_ids:
            return {
                "results": results,
                "all_completed": _check_all_completed(results),
            }

        # Register pending entries for agents still running
        entries = {}
        for aid in pending_ids:
            entry = self.engine.pending.register(session_id=aid, timeout=timeout)
            self._wait_pendings[aid] = entry.key
            entries[aid] = entry

        # Wait for all pending agents
        pending_tasks: set[asyncio.Task] = set()
        try:
            tasks = {
                aid: asyncio.create_task(self.engine.pending.wait(entry.key))
                for aid, entry in entries.items()
            }
            _done, pending_tasks = await asyncio.wait(tasks.values(), timeout=timeout)

            for aid, task in tasks.items():
                if task.done() and not task.cancelled():
                    try:
                        results[aid] = task.result()
                    except Exception as e:
                        results[aid] = {"status": "error", "error": str(e)}
                else:
                    results[aid] = {"status": "timeout"}
        except Exception as e:
            for aid in pending_ids:
                if aid not in results:
                    results[aid] = {"status": "error", "error": str(e)}
        finally:
            # Cancel any remaining tasks
            for t in pending_tasks:
                t.cancel()
            # Clean up pending mappings
            for aid in pending_ids:
                self._wait_pendings.pop(aid, None)

        return {
            "results": results,
            "all_completed": _check_all_completed(results),
        }

    # -- status --------------------------------------------------------------

    async def _handle_status(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        include_result = params.get("include_result", False)

        agent = self._store.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent {agent_id[:8]} not found"}

        result = {
            "agent_id": agent.id,
            "name": agent.name,
            "status": agent.status,
            "paradigm": agent.paradigm,
            "created_at": agent.created_at,
            "completed_at": agent.completed_at,
            "cost_usd": agent.cost_usd,
            "worktree_branch": agent.worktree_branch,
        }

        # Include diff stat if worktree is active
        if (
            agent.worktree_enabled
            and agent.worktree_path
            and agent.parent_branch
            and Path(agent.worktree_path).exists()
        ):
            try:
                stat = await get_worktree_diff_stat_only(agent.worktree_path, agent.parent_branch)
                result["diff_stat"] = stat
            except Exception:
                pass

        if include_result:
            result["result"] = agent.result_summary
            if agent.error:
                result["error"] = agent.error

        return result

    # -- send ----------------------------------------------------------------

    async def _handle_send(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        prompt = params.get("prompt", "")

        if not prompt:
            return {"error": "prompt is required"}

        agent = self._store.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent {agent_id[:8]} not found"}

        # Allow re-activating completed/stopped agents (session still exists as IDLE)
        if agent.status in ("completed", "stopped"):
            self._store.update_agent(agent_id, status="running")
        elif agent.status in ("failed", "merged"):
            return {"error": f"Cannot send to {agent.status} agent"}
        # running/pending → normal send (queue append)

        session = self.sm.sessions.get(agent_id)
        if not session:
            return {"error": f"Session {agent_id[:8]} no longer exists"}

        pos = await self.sm.send_prompt(agent_id, prompt)
        return {"sent": True, "queue_position": pos}

    # -- stop ----------------------------------------------------------------

    async def _handle_stop(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")

        agent = self._store.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent {agent_id[:8]} not found"}
        if agent.status not in ("pending", "running"):
            return {"error": f"Agent is {agent.status}, not running"}

        session = self.sm.sessions.get(agent_id)
        if not session:
            self._store.update_agent(agent_id, status="stopped")
            return {"stopped": True, "note": "Session already gone"}

        stopped, drained = await self.sm.stop_session(agent_id)
        # Note: delivery callback (_on_delivery) will also set status="stopped"
        # when it receives is_final+is_stopped. We set it here eagerly so the
        # caller gets an immediate consistent response, and the callback write
        # is an idempotent no-op.
        self._store.update_agent(
            agent_id,
            status="stopped",
            completed_at=datetime.now(UTC).isoformat(),
        )
        return {"stopped": True, "was_running": stopped, "drained": drained}

    # -- list ----------------------------------------------------------------

    async def _handle_list(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        agents = self._store.list_agents(parent_session_id=session_id)
        return {
            "agents": [
                {
                    "agent_id": a.id,
                    "name": a.name,
                    "status": a.status,
                    "paradigm": a.paradigm,
                    "worktree_branch": a.worktree_branch,
                    "cost_usd": a.cost_usd,
                }
                for a in agents
            ]
        }

    # -- diff ----------------------------------------------------------------

    async def _handle_diff(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")

        agent = self._store.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent {agent_id[:8]} not found"}
        if not agent.worktree_enabled or not agent.worktree_path:
            return {"error": "Agent has no worktree"}
        if not agent.parent_branch:
            return {"error": "No parent branch recorded"}
        if not Path(agent.worktree_path).exists():
            return {"error": "Worktree directory no longer exists"}

        return await get_worktree_diff(agent.worktree_path, agent.parent_branch)

    # -- merge ---------------------------------------------------------------

    async def _handle_merge(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")

        agent = self._store.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent {agent_id[:8]} not found"}
        if agent.status not in ("completed", "stopped"):
            return {"error": f"Cannot merge agent in status '{agent.status}'"}
        if not agent.worktree_enabled or not agent.worktree_branch:
            return {"error": "Agent has no worktree to merge"}
        if not agent.parent_branch:
            return {"error": "No parent branch recorded"}

        # Find the main repo dir.  Prefer live parent session; fall back to
        # the worktree's parent repo (git worktree metadata points back).
        parent_session = self.sm.sessions.get(agent.parent_session_id)
        if parent_session:
            repo_dir = parent_session.working_dir
        else:
            # Parent session gone — infer repo from worktree commondir
            from extensions.subagent.worktree import _run as _git_run

            rc, repo_path, _ = await _git_run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=agent.worktree_path,
            )
            if rc == 0 and repo_path:
                # --git-common-dir returns e.g. /path/to/repo/.git
                repo_dir = str(Path(repo_path).parent)
            else:
                return {"error": "Parent session gone and cannot determine repo path"}

        # Squash merge (validates branch + clean state internally)
        result = await squash_merge(repo_dir, agent.worktree_branch, agent.parent_branch)
        if "error" in result:
            return result

        # Mark as merged
        self._store.update_agent(agent_id, status="merged")

        # Cleanup worktree + branch + session
        if agent.worktree_path:
            await cleanup_worktree(repo_dir, agent.worktree_path, agent.worktree_branch)

        session = self.sm.sessions.get(agent_id)
        if session:
            await self.sm.destroy_session(agent_id)

        if self.engine.events:
            self.engine.events.log(
                "subagent.merged",
                session_id=agent_id,
                detail={
                    "branch": agent.worktree_branch,
                    "staged_files": result.get("staged_files", []),
                },
            )

        return {
            "merged": True,
            "staged_files": result.get("staged_files", []),
            "note": "Changes are staged but NOT committed. Review and commit manually.",
        }

    # -- slot reclamation ----------------------------------------------------

    async def _reclaim_session(self, user_id: str, parent_session_id: str) -> bool:
        """Try to destroy a completed sub-agent session to free a slot."""
        for s in self.sm.get_sessions_for_user(user_id):
            # Only reclaim sub-agent sessions (or auto-cleanup heartbeat/cron)
            if s.id == parent_session_id:
                continue
            is_subagent = s.context.get("subagent_worker")
            is_auto_cleanup = (
                s.context.get("subagent_auto_cleanup")
                or s.context.get("heartbeat_auto_cleanup")
                or s.context.get("cron_auto_cleanup")
            )
            if (is_subagent or is_auto_cleanup) and s.status in (
                SessionStatus.IDLE,
                SessionStatus.STOPPED,
            ):
                await self.sm.destroy_session(s.id)
                log.info("Reclaimed session %s to free slot for sub-agent", s.id[:8])
                return True
        return False
