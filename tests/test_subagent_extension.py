"""Tests for sub-agent extension lifecycle, spawn, delivery, cleanup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.pending import PendingStore
from core.session import SessionOverrides, SessionStatus
from extensions.subagent.extension import ExtensionImpl, _check_all_completed
from extensions.subagent.store import SubAgent


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture
def engine(tmp_path):
    """Minimal mock engine with session_manager and pending store."""
    engine = MagicMock()
    sm = engine.session_manager
    sm.base_dir = tmp_path / "sessions"
    sm.sessions = {}
    sm.max_sessions_per_user = 5
    sm.get_sessions_for_user = MagicMock(return_value=[])
    sm.create_session = AsyncMock()
    sm.send_prompt = AsyncMock(return_value=0)
    sm.stop_session = AsyncMock(return_value=(True, 0))
    sm.destroy_session = AsyncMock()
    sm.deliver = AsyncMock()

    engine.services = {}
    engine.events = MagicMock()
    engine.bridge = MagicMock()
    engine.pending = PendingStore()
    return engine


@pytest.fixture
def config():
    return {
        "max_subagents_per_session": 5,
        "default_paradigm": "coder",
        "cleanup_delay": 0.1,
    }


@pytest.fixture
def ext(engine, config):
    ext = ExtensionImpl()
    ext.configure(engine, config)
    return ext


def _mock_session(
    session_id="sess-001",
    user_id="user-1",
    working_dir="/tmp/work",
    context=None,
    status=SessionStatus.IDLE,
):
    s = MagicMock()
    s.id = session_id
    s.user_id = user_id
    s.working_dir = working_dir
    s.context = context or {}
    s.status = status
    return s


# -- Lifecycle: start -------------------------------------------------------


class TestSubagentStart:
    def test_start_creates_store_directory(self, ext, tmp_path):
        _run(ext.start())
        store_dir = tmp_path / "sessions" / "subagent"
        assert store_dir.exists()

    def test_start_registers_mcp_server(self, ext):
        _run(ext.start())
        ext.engine.session_manager.register_mcp_server.assert_called_once()
        call_args = ext.engine.session_manager.register_mcp_server.call_args
        assert call_args[0][0] == "subagent"

    def test_start_registers_system_prompt(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_system_prompt.assert_called_once()
        prompt = ext.engine.session_manager.add_system_prompt.call_args[0][0]
        assert "subagent_spawn" in prompt

    def test_start_registers_service(self, ext):
        _run(ext.start())
        assert ext.engine.services["subagent"] is ext

    def test_start_registers_delivery_callback(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_delivery_callback.assert_called_once()

    def test_start_registers_session_customizer(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_session_customizer.assert_called_once()

    def test_start_registers_bridge_handler(self, ext):
        _run(ext.start())
        ext.engine.bridge.add_handler.assert_called_once()


# -- Lifecycle: stop --------------------------------------------------------


class TestSubagentStop:
    def test_stop_removes_service(self, ext):
        _run(ext.start())
        assert "subagent" in ext.engine.services
        _run(ext.stop())
        assert "subagent" not in ext.engine.services

    def test_stop_resolves_pending_waits(self, ext):
        async def _test():
            await ext.start()
            # Simulate an active wait pending
            entry = ext.engine.pending.register(session_id="agent-x", timeout=60)
            ext._wait_pendings["agent-x"] = entry.key
            await ext.stop()
            assert ext._wait_pendings == {}

        asyncio.run(_test())


# -- Health check -----------------------------------------------------------


class TestSubagentHealth:
    def test_health_not_initialized(self, ext):
        result = _run(ext.health_check())
        assert result["status"] == "error"

    def test_health_ok(self, ext):
        _run(ext.start())
        result = _run(ext.health_check())
        assert result["status"] == "ok"
        assert result["total_agents"] == 0
        assert result["running"] == 0


# -- Session customizer -----------------------------------------------------


class TestSessionCustomizer:
    def test_non_worker_returns_none(self, ext):
        _run(ext.start())
        session = _mock_session(context={})
        result = ext._customize_session(session)
        assert result is None

    def test_worker_excludes_subagent_mcp(self, ext):
        _run(ext.start())
        session = _mock_session(
            context={
                "subagent_worker": True,
                "subagent_paradigm": "coder",
                "subagent_task": "Do stuff",
            }
        )
        result = ext._customize_session(session)
        assert isinstance(result, SessionOverrides)
        assert "subagent" in result.exclude_mcp_servers

    def test_reviewer_disallows_write_tools(self, ext):
        _run(ext.start())
        session = _mock_session(
            context={
                "subagent_worker": True,
                "subagent_paradigm": "reviewer",
                "subagent_task": "Review code",
            }
        )
        result = ext._customize_session(session)
        assert "Write" in result.extra_disallowed_tools
        assert "Edit" in result.extra_disallowed_tools

    def test_coder_no_disallowed_tools(self, ext):
        _run(ext.start())
        session = _mock_session(
            context={
                "subagent_worker": True,
                "subagent_paradigm": "coder",
                "subagent_task": "Write code",
            }
        )
        result = ext._customize_session(session)
        assert result.extra_disallowed_tools is None

    def test_worker_gets_task_in_system_prompt(self, ext):
        _run(ext.start())
        session = _mock_session(
            context={
                "subagent_worker": True,
                "subagent_paradigm": "coder",
                "subagent_task": "Optimize the database",
            }
        )
        result = ext._customize_session(session)
        prompt_text = "\n".join(result.extra_system_prompt)
        assert "Optimize the database" in prompt_text


# -- Bridge handler: spawn --------------------------------------------------


class TestSpawn:
    def test_spawn_success(self, ext):
        async def _test():
            await ext.start()
            parent = _mock_session()
            ext.sm.sessions["sess-001"] = parent
            worker = _mock_session(session_id="worker-001")
            ext.sm.create_session = AsyncMock(return_value=worker)

            result = await ext._handle_spawn(
                {
                    "session_id": "sess-001",
                    "task": "Write unit tests",
                    "name": "test-writer",
                    "worktree": False,
                    "paradigm": "coder",
                }
            )
            assert "error" not in result
            assert result["agent_id"] == "worker-001"
            assert result["name"] == "test-writer"
            assert result["status"] == "running"

            # Verify session created
            ext.sm.create_session.assert_called_once()

            # Verify agent stored
            agent = ext._store.get_agent("worker-001")
            assert agent is not None
            assert agent.task == "Write unit tests"
            assert agent.status == "running"

        asyncio.run(_test())

    def test_spawn_no_task(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_spawn(
                {
                    "session_id": "sess-001",
                }
            )
            assert "error" in result
            assert "task" in result["error"].lower()

        asyncio.run(_test())

    def test_spawn_parent_not_found(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_spawn(
                {
                    "session_id": "nonexistent",
                    "task": "Do stuff",
                }
            )
            assert "error" in result
            assert "not found" in result["error"].lower()

        asyncio.run(_test())

    def test_spawn_max_subagents_reached(self, ext):
        async def _test():
            await ext.start()
            parent = _mock_session()
            ext.sm.sessions["sess-001"] = parent

            # Fill up sub-agent slots
            for i in range(5):
                ext._store.add_agent(
                    SubAgent(
                        id=f"w-{i}",
                        parent_session_id="sess-001",
                        name=f"w{i}",
                        task="t",
                        paradigm="coder",
                        user_id="user-1",
                        working_dir="/tmp",
                        status="running",
                    )
                )

            result = await ext._handle_spawn(
                {
                    "session_id": "sess-001",
                    "task": "One more",
                }
            )
            assert "error" in result
            assert "max" in result["error"].lower()

        asyncio.run(_test())

    def test_spawn_inherits_parent_context(self, ext):
        async def _test():
            await ext.start()
            parent = _mock_session(context={"chat_id": 12345})
            ext.sm.sessions["sess-001"] = parent
            worker = _mock_session(session_id="worker-001")
            ext.sm.create_session = AsyncMock(return_value=worker)

            await ext._handle_spawn(
                {
                    "session_id": "sess-001",
                    "task": "Work",
                }
            )

            call_kwargs = ext.sm.create_session.call_args
            context = call_kwargs.kwargs.get("context") or call_kwargs[1].get("context")
            assert context["chat_id"] == 12345
            assert context["subagent_worker"] is True

        asyncio.run(_test())


# -- Bridge handler: list ---------------------------------------------------


class TestList:
    def test_list_empty(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_list({"session_id": "sess-001"})
            assert result["agents"] == []

        asyncio.run(_test())

    def test_list_with_agents(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="sess-001",
                    name="worker-1",
                    task="t1",
                    paradigm="coder",
                    user_id="u1",
                    working_dir="/tmp",
                    status="running",
                )
            )
            result = await ext._handle_list({"session_id": "sess-001"})
            assert len(result["agents"]) == 1
            assert result["agents"][0]["name"] == "worker-1"

        asyncio.run(_test())


# -- Bridge handler: status -------------------------------------------------


class TestStatus:
    def test_status_not_found(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_status({"agent_id": "nonexistent"})
            assert "error" in result

        asyncio.run(_test())

    def test_status_basic(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    cost_usd=0.05,
                )
            )
            result = await ext._handle_status({"agent_id": "w1"})
            assert result["status"] == "completed"
            assert result["cost_usd"] == 0.05

        asyncio.run(_test())

    def test_status_include_result_no_false_error(self, ext):
        """include_result=True should NOT inject 'error' key when agent.error is None."""

        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    result_summary="All good",
                    cost_usd=0.05,
                    error=None,
                )
            )
            result = await ext._handle_status({"agent_id": "w1", "include_result": True})
            assert "error" not in result
            assert result["result"] == "All good"
            assert result["status"] == "completed"

        asyncio.run(_test())

    def test_status_include_result_with_error(self, ext):
        """include_result=True should include 'error' when agent has one."""

        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="failed",
                    error="Something broke",
                )
            )
            result = await ext._handle_status({"agent_id": "w1", "include_result": True})
            assert result["error"] == "Something broke"

        asyncio.run(_test())


# -- Bridge handler: send ---------------------------------------------------


class TestSend:
    def test_send_to_running(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            ext.sm.sessions["w1"] = _mock_session(session_id="w1")

            result = await ext._handle_send({"agent_id": "w1", "prompt": "Do more"})
            assert result["sent"] is True

        asyncio.run(_test())

    def test_send_reactivates_completed(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="reviewer",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                )
            )
            ext.sm.sessions["w1"] = _mock_session(session_id="w1")

            result = await ext._handle_send({"agent_id": "w1", "prompt": "Review v2"})
            assert result["sent"] is True
            agent = ext._store.get_agent("w1")
            assert agent.status == "running"

        asyncio.run(_test())

    def test_send_to_failed_rejected(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="failed",
                )
            )
            result = await ext._handle_send({"agent_id": "w1", "prompt": "Retry"})
            assert "error" in result

        asyncio.run(_test())


# -- Bridge handler: stop ---------------------------------------------------


class TestStop:
    def test_stop_running_agent(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            ext.sm.sessions["w1"] = _mock_session(session_id="w1")

            result = await ext._handle_stop({"agent_id": "w1"})
            assert result["stopped"] is True
            agent = ext._store.get_agent("w1")
            assert agent.status == "stopped"

        asyncio.run(_test())

    def test_stop_already_completed(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                )
            )
            result = await ext._handle_stop({"agent_id": "w1"})
            assert "error" in result

        asyncio.run(_test())


# -- Delivery callback ------------------------------------------------------


class TestDeliveryCallback:
    def test_delivery_marks_completed(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            session = _mock_session(
                session_id="w1",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "p1",
                    "subagent_paradigm": "coder",
                },
            )
            ext.sm.sessions["w1"] = session

            await ext._on_delivery(
                "w1",
                "Task done.",
                {
                    "is_final": True,
                    "total_cost_usd": 0.05,
                },
            )

            agent = ext._store.get_agent("w1")
            assert agent.status == "completed"
            assert agent.cost_usd == 0.05
            assert agent.result_summary == "Task done."

        asyncio.run(_test())

    def test_delivery_error_marks_failed(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            session = _mock_session(
                session_id="w1",
                context={"subagent_worker": True, "subagent_parent_id": "p1"},
            )
            ext.sm.sessions["w1"] = session

            await ext._on_delivery(
                "w1",
                "Error occurred",
                {
                    "is_final": True,
                    "is_error": True,
                },
            )

            agent = ext._store.get_agent("w1")
            assert agent.status == "failed"

        asyncio.run(_test())

    def test_delivery_resolves_pending_wait(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            session = _mock_session(
                session_id="w1",
                context={"subagent_worker": True, "subagent_parent_id": "p1"},
            )
            ext.sm.sessions["w1"] = session

            # Register a pending wait
            entry = ext.engine.pending.register(session_id="w1", timeout=60)
            ext._wait_pendings["w1"] = entry.key

            # Trigger delivery
            await ext._on_delivery(
                "w1",
                "Done.",
                {
                    "is_final": True,
                    "total_cost_usd": 0.03,
                },
            )

            # Pending should be resolved
            result = entry.future.result()
            assert result["status"] == "completed"
            assert result["cost_usd"] == 0.03

        asyncio.run(_test())

    def test_delivery_notifies_parent(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="my-worker",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            session = _mock_session(
                session_id="w1",
                context={"subagent_worker": True, "subagent_parent_id": "p1"},
            )
            ext.sm.sessions["w1"] = session
            ext.sm.sessions["p1"] = _mock_session(session_id="p1")

            await ext._on_delivery("w1", "Done.", {"is_final": True})

            # Should have called deliver on parent session
            ext.sm.deliver.assert_called_once()
            call_args = ext.sm.deliver.call_args
            assert call_args[0][0] == "p1"  # parent session_id
            assert "my-worker" in call_args[0][1]

        asyncio.run(_test())

    def test_delivery_non_worker_ignored(self, ext):
        async def _test():
            await ext.start()
            session = _mock_session(session_id="regular", context={})
            ext.sm.sessions["regular"] = session

            await ext._on_delivery("regular", "Done.", {"is_final": True})
            # Should not crash or interact with store

        asyncio.run(_test())

    def test_delivery_non_final_ignored(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            session = _mock_session(
                session_id="w1",
                context={"subagent_worker": True},
            )
            ext.sm.sessions["w1"] = session

            await ext._on_delivery("w1", "streaming...", {"is_stream": True})

            agent = ext._store.get_agent("w1")
            assert agent.status == "running"  # unchanged

        asyncio.run(_test())


# -- Recovery ---------------------------------------------------------------


class TestRecovery:
    def test_recovery_marks_dead_sessions_failed(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            # No session in sm.sessions → treated as dead

            await ext._recover()

            agent = ext._store.get_agent("w1")
            assert agent.status == "failed"
            assert "restart" in agent.error.lower()

        asyncio.run(_test())

    def test_recovery_keeps_alive_sessions(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            ext.sm.sessions["w1"] = _mock_session(session_id="w1", status=SessionStatus.BUSY)

            await ext._recover()

            agent = ext._store.get_agent("w1")
            assert agent.status == "running"  # unchanged

        asyncio.run(_test())


# -- Wait handler -----------------------------------------------------------


class TestWait:
    def test_wait_all_already_completed(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w1",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    result_summary="Done",
                    cost_usd=0.01,
                )
            )
            ext._store.add_agent(
                SubAgent(
                    id="w2",
                    parent_session_id="p1",
                    name="w2",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    result_summary="Also done",
                    cost_usd=0.02,
                )
            )

            result = await ext._handle_wait(
                {
                    "agent_ids": ["w1", "w2"],
                    "timeout": 5,
                }
            )
            assert result["all_completed"] is True
            assert result["results"]["w1"]["status"] == "completed"
            assert result["results"]["w2"]["status"] == "completed"

        asyncio.run(_test())

    def test_wait_not_found(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_wait(
                {
                    "agent_ids": ["nonexistent"],
                    "timeout": 5,
                }
            )
            assert result["results"]["nonexistent"]["error"] == "not found"
            # all_completed should be False when all are "not found"
            assert result["all_completed"] is False

        asyncio.run(_test())

    def test_wait_mixed_completed_and_running(self, ext):
        """Test wait with one completed and one that completes during wait."""

        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w1",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    result_summary="Done",
                )
            )
            ext._store.add_agent(
                SubAgent(
                    id="w2",
                    parent_session_id="p1",
                    name="w2",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )

            # Simulate resolving w2 after a brief delay
            async def _resolve_later():
                await asyncio.sleep(0.1)
                # Find the pending key for w2
                key = ext._wait_pendings.get("w2")
                if key:
                    ext.engine.pending.resolve(
                        key,
                        {
                            "status": "completed",
                            "result": "Late done",
                            "cost_usd": 0.03,
                        },
                    )

            resolve_task = asyncio.create_task(_resolve_later())

            result = await ext._handle_wait(
                {
                    "agent_ids": ["w1", "w2"],
                    "timeout": 5,
                }
            )

            await resolve_task

            assert result["results"]["w1"]["status"] == "completed"
            assert result["results"]["w2"]["status"] == "completed"
            assert result["all_completed"] is True

        asyncio.run(_test())

    def test_wait_timeout(self, ext):
        """Test that wait returns timeout for agents that don't complete."""

        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w1",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )

            result = await ext._handle_wait(
                {
                    "agent_ids": ["w1"],
                    "timeout": 0.2,
                }
            )

            assert result["results"]["w1"]["status"] == "timeout"
            assert result["all_completed"] is False

        asyncio.run(_test())

    def test_wait_empty_ids(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_wait({"agent_ids": []})
            assert "error" in result

        asyncio.run(_test())


# -- Slot reclamation -------------------------------------------------------


class TestSlotReclamation:
    def test_reclaim_idle_subagent_session(self, ext):
        async def _test():
            await ext.start()
            idle = _mock_session(
                session_id="old-worker",
                context={"subagent_worker": True, "subagent_auto_cleanup": True},
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[idle])

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is True
            ext.sm.destroy_session.assert_called_once_with("old-worker")

        asyncio.run(_test())

    def test_no_reclaim_busy_session(self, ext):
        async def _test():
            await ext.start()
            busy = _mock_session(
                session_id="busy-worker",
                context={"subagent_worker": True},
                status=SessionStatus.BUSY,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[busy])

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is False

        asyncio.run(_test())

    def test_no_reclaim_parent_session(self, ext):
        async def _test():
            await ext.start()
            parent = _mock_session(
                session_id="parent-001",
                context={"subagent_auto_cleanup": True},
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[parent])

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is False

        asyncio.run(_test())

    def test_reclaim_prefers_own_over_others(self, ext):
        """Phase 1 (own worker) should be preferred over phase 2 (negotiation)."""

        async def _test():
            await ext.start()
            # Own idle worker
            own = _mock_session(
                session_id="own-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-001",
                },
                status=SessionStatus.IDLE,
            )
            # Another parent's idle worker
            other = _mock_session(
                session_id="other-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-002",
                },
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[own, other])

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is True
            # Should reclaim own worker, not negotiate with other parent
            ext.sm.destroy_session.assert_called_once_with("own-worker")

        asyncio.run(_test())

    def test_reclaim_negotiates_with_other_parent(self, ext):
        """Phase 2: negotiates with other parent when no own sessions to reclaim."""

        async def _test():
            await ext.start()
            # Another parent's idle worker (not auto-cleanup, not own)
            other_worker = _mock_session(
                session_id="other-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-002",
                },
                status=SessionStatus.IDLE,
            )
            other_parent = _mock_session(
                session_id="parent-002",
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[other_worker])
            ext.sm.sessions["parent-002"] = other_parent

            # Store agent record for the other worker
            ext._store.add_agent(
                SubAgent(
                    id="other-worker",
                    parent_session_id="parent-002",
                    name="old-task",
                    task="t",
                    paradigm="coder",
                    user_id="user-1",
                    working_dir="/tmp",
                    status="completed",
                )
            )

            # Simulate approval: resolve pending after send_prompt is called
            async def auto_approve(session_id, prompt):
                # Find and approve the pending entry
                for entry in ext.engine.pending._entries.values():
                    if (
                        entry.data.get("type") == "subagent_reclaim"
                        and entry.session_id == session_id
                    ):
                        ext.engine.pending.resolve(entry.key, {"approved": True})
                        break
                return 0

            ext.sm.send_prompt = AsyncMock(side_effect=auto_approve)

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is True
            ext.sm.destroy_session.assert_called_once_with("other-worker")
            ext.sm.send_prompt.assert_called_once()
            # Verify prompt was sent to the other parent
            call_args = ext.sm.send_prompt.call_args
            assert call_args[0][0] == "parent-002"
            assert "Reclamation Request" in call_args[0][1]

        asyncio.run(_test())

    def test_reclaim_negotiation_denied(self, ext):
        """Phase 2: denied negotiation returns False."""

        async def _test():
            await ext.start()
            other_worker = _mock_session(
                session_id="other-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-002",
                },
                status=SessionStatus.IDLE,
            )
            other_parent = _mock_session(
                session_id="parent-002",
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[other_worker])
            ext.sm.sessions["parent-002"] = other_parent

            ext._store.add_agent(
                SubAgent(
                    id="other-worker",
                    parent_session_id="parent-002",
                    name="old-task",
                    task="t",
                    paradigm="coder",
                    user_id="user-1",
                    working_dir="/tmp",
                    status="completed",
                )
            )

            async def auto_deny(session_id, prompt):
                for entry in ext.engine.pending._entries.values():
                    if entry.data.get("type") == "subagent_reclaim":
                        ext.engine.pending.resolve(entry.key, {"approved": False})
                        break
                return 0

            ext.sm.send_prompt = AsyncMock(side_effect=auto_deny)

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is False
            ext.sm.destroy_session.assert_not_called()

        asyncio.run(_test())

    def test_reclaim_negotiation_timeout(self, ext):
        """Phase 2: timeout returns False without destroying."""

        async def _test():
            await ext.start()
            ext._RECLAIM_TIMEOUT = 0.1  # very short for test

            other_worker = _mock_session(
                session_id="other-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-002",
                },
                status=SessionStatus.IDLE,
            )
            other_parent = _mock_session(
                session_id="parent-002",
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[other_worker])
            ext.sm.sessions["parent-002"] = other_parent

            ext._store.add_agent(
                SubAgent(
                    id="other-worker",
                    parent_session_id="parent-002",
                    name="old-task",
                    task="t",
                    paradigm="coder",
                    user_id="user-1",
                    working_dir="/tmp",
                    status="completed",
                )
            )

            # Don't resolve — let it timeout
            ext.sm.send_prompt = AsyncMock(return_value=0)

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is False
            ext.sm.destroy_session.assert_not_called()

        asyncio.run(_test())

    def test_reclaim_skips_busy_other_parent(self, ext):
        """Phase 2: skip negotiation if other parent is BUSY."""

        async def _test():
            await ext.start()
            other_worker = _mock_session(
                session_id="other-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-002",
                },
                status=SessionStatus.IDLE,
            )
            busy_parent = _mock_session(
                session_id="parent-002",
                status=SessionStatus.BUSY,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[other_worker])
            ext.sm.sessions["parent-002"] = busy_parent

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is False
            ext.sm.send_prompt.assert_not_called()

        asyncio.run(_test())

    def test_reclaim_send_prompt_failure(self, ext):
        """Phase 2: if send_prompt fails, return False gracefully."""

        async def _test():
            await ext.start()
            other_worker = _mock_session(
                session_id="other-worker",
                context={
                    "subagent_worker": True,
                    "subagent_parent_id": "parent-002",
                },
                status=SessionStatus.IDLE,
            )
            other_parent = _mock_session(
                session_id="parent-002",
                status=SessionStatus.IDLE,
            )
            ext.sm.get_sessions_for_user = MagicMock(return_value=[other_worker])
            ext.sm.sessions["parent-002"] = other_parent

            ext._store.add_agent(
                SubAgent(
                    id="other-worker",
                    parent_session_id="parent-002",
                    name="old-task",
                    task="t",
                    paradigm="coder",
                    user_id="user-1",
                    working_dir="/tmp",
                    status="completed",
                )
            )

            ext.sm.send_prompt = AsyncMock(side_effect=RuntimeError("Session gone"))

            reclaimed = await ext._reclaim_session("user-1", "parent-001")
            assert reclaimed is False
            ext.sm.destroy_session.assert_not_called()

        asyncio.run(_test())


# -- Bridge handler: reclaim_respond ----------------------------------------


class TestReclaimRespond:
    def test_respond_missing_request_id(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_reclaim_respond({"session_id": "s1", "approve": True})
            assert "error" in result
            assert "request_id" in result["error"]

        asyncio.run(_test())

    def test_respond_unknown_request_id(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_reclaim_respond(
                {"session_id": "s1", "request_id": "nonexistent", "approve": True}
            )
            assert "error" in result
            assert "timed out" in result["error"]

        asyncio.run(_test())

    def test_respond_wrong_type(self, ext):
        async def _test():
            await ext.start()
            entry = ext.engine.pending.register(session_id="s1", data={"type": "ask_user"})
            result = await ext._handle_reclaim_respond(
                {"session_id": "s1", "request_id": entry.key, "approve": True}
            )
            assert "error" in result
            assert "does not correspond" in result["error"]

        asyncio.run(_test())

    def test_respond_wrong_session(self, ext):
        async def _test():
            await ext.start()
            entry = ext.engine.pending.register(session_id="s1", data={"type": "subagent_reclaim"})
            result = await ext._handle_reclaim_respond(
                {"session_id": "s2", "request_id": entry.key, "approve": True}
            )
            assert "error" in result
            assert "not you" in result["error"]

        asyncio.run(_test())

    def test_respond_approve(self, ext):
        async def _test():
            await ext.start()
            entry = ext.engine.pending.register(
                session_id="s1",
                data={"type": "subagent_reclaim", "candidate_id": "w1"},
            )
            result = await ext._handle_reclaim_respond(
                {"session_id": "s1", "request_id": entry.key, "approve": True}
            )
            assert result["resolved"] is True
            assert result["approved"] is True

        asyncio.run(_test())

    def test_respond_deny(self, ext):
        async def _test():
            await ext.start()
            entry = ext.engine.pending.register(
                session_id="s1",
                data={"type": "subagent_reclaim", "candidate_id": "w1"},
            )
            result = await ext._handle_reclaim_respond(
                {"session_id": "s1", "request_id": entry.key, "approve": False}
            )
            assert result["resolved"] is True
            assert result["approved"] is False

        asyncio.run(_test())

    def test_respond_already_resolved(self, ext):
        async def _test():
            await ext.start()
            entry = ext.engine.pending.register(
                session_id="s1",
                data={"type": "subagent_reclaim", "candidate_id": "w1"},
            )
            ext.engine.pending.resolve(entry.key, {"approved": True})
            result = await ext._handle_reclaim_respond(
                {"session_id": "s1", "request_id": entry.key, "approve": True}
            )
            assert "error" in result
            assert "already resolved" in result["error"]

        asyncio.run(_test())


# -- Bridge handler: diff ---------------------------------------------------


class TestDiff:
    def test_diff_not_found(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_diff({"agent_id": "nonexistent"})
            assert "error" in result

        asyncio.run(_test())

    def test_diff_no_worktree(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    worktree_enabled=False,
                )
            )
            result = await ext._handle_diff({"agent_id": "w1"})
            assert "error" in result
            assert "no worktree" in result["error"].lower()

        asyncio.run(_test())

    def test_diff_worktree_missing_on_disk(self, ext, tmp_path):
        async def _test():
            await ext.start()
            gone = str(tmp_path / "gone")
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    worktree_enabled=True,
                    worktree_path=gone,
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            result = await ext._handle_diff({"agent_id": "w1"})
            assert "error" in result
            assert "no longer exists" in result["error"].lower()

        asyncio.run(_test())

    @patch("extensions.subagent.extension.get_worktree_diff")
    def test_diff_success(self, mock_diff, ext, tmp_path):
        async def _test():
            await ext.start()
            wt_dir = tmp_path / "wt"
            wt_dir.mkdir()
            mock_diff.return_value = {
                "stat": " file.py | 1 +",
                "diff": "+print('hi')",
                "truncated": False,
            }
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    worktree_enabled=True,
                    worktree_path=str(wt_dir),
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            result = await ext._handle_diff({"agent_id": "w1"})
            assert "error" not in result
            assert "file.py" in result["stat"]
            assert result["truncated"] is False
            mock_diff.assert_called_once_with(str(wt_dir), "main")

        asyncio.run(_test())


# -- Bridge handler: merge --------------------------------------------------


class TestMerge:
    def test_merge_not_found(self, ext):
        async def _test():
            await ext.start()
            result = await ext._handle_merge({"agent_id": "nonexistent"})
            assert "error" in result

        asyncio.run(_test())

    def test_merge_wrong_status(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                    worktree_enabled=True,
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            result = await ext._handle_merge({"agent_id": "w1"})
            assert "error" in result
            assert "running" in result["error"]

        asyncio.run(_test())

    def test_merge_no_worktree(self, ext):
        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    worktree_enabled=False,
                )
            )
            result = await ext._handle_merge({"agent_id": "w1"})
            assert "error" in result
            assert "no worktree" in result["error"].lower()

        asyncio.run(_test())

    def test_merge_parent_session_gone_no_git(self, ext):
        """When parent gone and git commondir also fails, return error."""

        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p-gone",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="completed",
                    worktree_enabled=True,
                    worktree_path="/tmp/gone-wt",
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            with patch("extensions.subagent.worktree._run") as mock_git:
                mock_git.return_value = (128, "", "not a git repo")
                result = await ext._handle_merge({"agent_id": "w1"})
            assert "error" in result
            assert "cannot determine" in result["error"].lower()

        asyncio.run(_test())

    @patch("extensions.subagent.extension.cleanup_worktree")
    @patch("extensions.subagent.extension.squash_merge")
    def test_merge_success(self, mock_merge, mock_cleanup, ext):
        async def _test():
            await ext.start()
            mock_merge.return_value = {"staged_files": ["feature.py", "test.py"]}
            mock_cleanup.return_value = {"removed": True}

            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp/wt",
                    status="completed",
                    worktree_enabled=True,
                    worktree_path="/tmp/wt",
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            parent = _mock_session(session_id="p1", working_dir="/tmp/repo")
            ext.sm.sessions["p1"] = parent

            result = await ext._handle_merge({"agent_id": "w1"})

            assert "error" not in result
            assert result["merged"] is True
            assert "feature.py" in result["staged_files"]
            assert len(result["staged_files"]) == 2

            # Verify store updated to merged
            agent = ext._store.get_agent("w1")
            assert agent.status == "merged"

            # Verify worktree cleanup called
            mock_cleanup.assert_called_once_with("/tmp/repo", "/tmp/wt", "subagent/w-1234")

        asyncio.run(_test())

    @patch("extensions.subagent.extension.squash_merge")
    def test_merge_conflict_propagated(self, mock_merge, ext):
        async def _test():
            await ext.start()
            mock_merge.return_value = {"error": "Merge failed: conflict in file.py"}

            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp/wt",
                    status="completed",
                    worktree_enabled=True,
                    worktree_path="/tmp/wt",
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            parent = _mock_session(session_id="p1", working_dir="/tmp/repo")
            ext.sm.sessions["p1"] = parent

            result = await ext._handle_merge({"agent_id": "w1"})

            assert "error" in result
            assert "conflict" in result["error"].lower()

            # Status should NOT change to merged on failure
            agent = ext._store.get_agent("w1")
            assert agent.status == "completed"

        asyncio.run(_test())


# -- Delivery: F1 fix — _wait_pendings cleanup on resolve ------------------


class TestDeliveryPendingCleanup:
    def test_delivery_removes_wait_pending_on_resolve(self, ext):
        """F1: _on_delivery should pop (not just get) the pending key."""

        async def _test():
            await ext.start()
            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p1",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp",
                    status="running",
                )
            )
            session = _mock_session(
                session_id="w1",
                context={"subagent_worker": True, "subagent_parent_id": "p1"},
            )
            ext.sm.sessions["w1"] = session

            entry = ext.engine.pending.register(session_id="w1", timeout=60)
            ext._wait_pendings["w1"] = entry.key

            await ext._on_delivery("w1", "Done.", {"is_final": True})

            # Key should be removed from _wait_pendings
            assert "w1" not in ext._wait_pendings

        asyncio.run(_test())


# -- _check_all_completed helper -------------------------------------------


class TestCheckAllCompleted:
    def test_empty_results(self):
        assert _check_all_completed({}) is False

    def test_all_completed(self):
        results = {
            "a": {"status": "completed"},
            "b": {"status": "merged"},
        }
        assert _check_all_completed(results) is True

    def test_all_errors(self):
        """Vacuous truth: when all entries have errors, should return False."""
        results = {
            "a": {"error": "not found"},
            "b": {"error": "not found"},
        }
        assert _check_all_completed(results) is False

    def test_mixed_errors_and_completed(self):
        results = {
            "a": {"status": "completed"},
            "b": {"error": "not found"},
        }
        assert _check_all_completed(results) is False

    def test_some_timeout(self):
        results = {
            "a": {"status": "completed"},
            "b": {"status": "timeout"},
        }
        assert _check_all_completed(results) is False

    def test_all_running(self):
        results = {
            "a": {"status": "running"},
        }
        assert _check_all_completed(results) is False


# -- Worker system prompt includes working_dir ----------------------------


class TestWorkerPromptWorkingDir:
    def test_worker_prompt_includes_working_dir(self, ext):
        _run(ext.start())
        session = _mock_session(
            working_dir="/home/user/worktree/branch-abc",
            context={
                "subagent_worker": True,
                "subagent_paradigm": "coder",
                "subagent_task": "Fix bug",
            },
        )
        result = ext._customize_session(session)
        prompt_text = "\n".join(result.extra_system_prompt)
        assert "/home/user/worktree/branch-abc" in prompt_text
        assert "MUST use this directory" in prompt_text

    def test_worker_prompt_includes_branch(self, ext):
        _run(ext.start())
        session = _mock_session(
            working_dir="/home/user/worktree/branch-abc",
            context={
                "subagent_worker": True,
                "subagent_paradigm": "coder",
                "subagent_task": "Fix bug",
                "subagent_worktree_branch": "subagent/fix-bug-abc12345",
            },
        )
        result = ext._customize_session(session)
        prompt_text = "\n".join(result.extra_system_prompt)
        assert "subagent/fix-bug-abc12345" in prompt_text
        assert "Do NOT switch branches" in prompt_text

    def test_worker_prompt_no_branch_when_no_worktree(self, ext):
        _run(ext.start())
        session = _mock_session(
            working_dir="/home/user/project",
            context={
                "subagent_worker": True,
                "subagent_paradigm": "coder",
                "subagent_task": "Fix bug",
            },
        )
        result = ext._customize_session(session)
        prompt_text = "\n".join(result.extra_system_prompt)
        assert "/home/user/project" in prompt_text
        assert "Do NOT switch branches" not in prompt_text


# -- Merge: parent session gone fallback -----------------------------------


class TestMergeParentGoneFallback:
    @patch("extensions.subagent.extension.cleanup_worktree")
    @patch("extensions.subagent.extension.squash_merge")
    def test_merge_parent_gone_uses_git_commondir(self, mock_merge, mock_cleanup, ext):
        """When parent session is destroyed, merge infers repo from git."""

        async def _test():
            await ext.start()
            mock_merge.return_value = {"staged_files": ["f.py"]}
            mock_cleanup.return_value = {"removed": True}

            ext._store.add_agent(
                SubAgent(
                    id="w1",
                    parent_session_id="p-gone",
                    name="w",
                    task="t",
                    paradigm="coder",
                    user_id="u",
                    working_dir="/tmp/wt",
                    status="completed",
                    worktree_enabled=True,
                    worktree_path="/tmp/wt",
                    worktree_branch="subagent/w-1234",
                    parent_branch="main",
                )
            )
            # No parent session in sm.sessions

            with patch("extensions.subagent.worktree._run") as mock_git:
                mock_git.return_value = (0, "/tmp/repo/.git", "")
                result = await ext._handle_merge({"agent_id": "w1"})

            assert "error" not in result
            assert result["merged"] is True
            mock_merge.assert_called_once_with("/tmp/repo", "subagent/w-1234", "main")

        asyncio.run(_test())
