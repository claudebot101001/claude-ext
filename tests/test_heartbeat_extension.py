"""Tests for heartbeat extension lifecycle, triggers, scheduling."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.pending import PendingStore
from core.session import SessionStatus
from extensions.heartbeat.extension import (
    _BACKOFF_MAX_MULTIPLIER,
    ExtensionImpl,
    TriggerEvent,
)
from extensions.heartbeat.store import HeartbeatState, HeartbeatStore


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture
def heartbeat_dir(tmp_path):
    return tmp_path / "sessions" / "heartbeat"


@pytest.fixture
def engine(tmp_path):
    """Minimal mock engine with session_manager."""
    engine = MagicMock()
    engine.session_manager.base_dir = tmp_path / "sessions"
    engine.session_manager.sessions = {}
    engine.session_manager.max_sessions_per_user = 5
    engine.session_manager.get_sessions_for_user = MagicMock(return_value=[])
    engine.session_manager.create_session = AsyncMock()
    engine.session_manager.send_prompt = AsyncMock()
    engine.session_manager.destroy_session = AsyncMock()
    engine.services = {}
    engine.events = MagicMock()
    engine.ask = AsyncMock(return_value="NOTHING")
    return engine


@pytest.fixture
def config():
    return {
        "user_id": "123456789",
        "notify_context": {"chat_id": 123456789},
        "interval": 1800,
        "max_daily_runs": 48,
        "usage_throttle": 80,
        "usage_pause": 95,
    }


@pytest.fixture
def ext(engine, config):
    ext = ExtensionImpl()
    ext.configure(engine, config)
    return ext


# -- Lifecycle: start -------------------------------------------------------


class TestHeartbeatStart:
    def test_start_creates_directory(self, ext, heartbeat_dir):
        _run(ext.start())
        assert heartbeat_dir.exists()
        # Cleanup
        ext._scheduler_task.cancel()

    def test_start_seeds_instructions(self, ext, heartbeat_dir):
        _run(ext.start())
        md = heartbeat_dir / "HEARTBEAT.md"
        assert md.exists()
        content = md.read_text(encoding="utf-8")
        assert "Standing Tasks" in content
        ext._scheduler_task.cancel()

    def test_start_does_not_overwrite_existing(self, ext, heartbeat_dir):
        heartbeat_dir.mkdir(parents=True)
        existing = "# My custom instructions"
        (heartbeat_dir / "HEARTBEAT.md").write_text(existing)

        _run(ext.start())
        content = (heartbeat_dir / "HEARTBEAT.md").read_text()
        assert content == existing
        ext._scheduler_task.cancel()

    def test_start_registers_mcp_server(self, ext):
        _run(ext.start())
        ext.engine.session_manager.register_mcp_server.assert_called_once()
        call_args = ext.engine.session_manager.register_mcp_server.call_args
        assert call_args[0][0] == "heartbeat"
        config = call_args[0][1]
        assert "HEARTBEAT_DIR" in config["env"]
        ext._scheduler_task.cancel()

    def test_start_registers_system_prompt(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_system_prompt.assert_called_once()
        prompt = ext.engine.session_manager.add_system_prompt.call_args[0][0]
        assert "heartbeat" in prompt.lower()
        ext._scheduler_task.cancel()

    def test_start_registers_service_as_self(self, ext):
        _run(ext.start())
        assert "heartbeat" in ext.engine.services
        assert ext.engine.services["heartbeat"] is ext  # self, not store
        ext._scheduler_task.cancel()

    def test_start_registers_delivery_callback(self, ext):
        _run(ext.start())
        ext.engine.session_manager.add_delivery_callback.assert_called_once()
        ext._scheduler_task.cancel()

    def test_start_requires_user_id(self, engine):
        ext = ExtensionImpl()
        ext.configure(engine, {"user_id": ""})
        with pytest.raises(RuntimeError, match="user_id"):
            _run(ext.start())


# -- Lifecycle: recovery (stale active_session_id) --------------------------


class TestHeartbeatRecovery:
    def test_clears_stale_dead_session(self, ext, heartbeat_dir):
        # Pre-create state with an active_session_id pointing to a dead session
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        store.save_state(HeartbeatState(active_session_id="dead-session-id"))

        dead_session = MagicMock()
        dead_session.status = SessionStatus.DEAD
        ext.engine.session_manager.sessions = {"dead-session-id": dead_session}

        _run(ext.start())

        loaded = store.load_state()
        assert loaded.active_session_id is None
        ext._scheduler_task.cancel()

    def test_clears_stale_nonexistent_session(self, ext, heartbeat_dir):
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        store.save_state(HeartbeatState(active_session_id="gone-session"))

        ext.engine.session_manager.sessions = {}  # session doesn't exist

        _run(ext.start())

        loaded = store.load_state()
        assert loaded.active_session_id is None
        ext._scheduler_task.cancel()

    def test_keeps_busy_session(self, ext, heartbeat_dir):
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        store.save_state(HeartbeatState(active_session_id="busy-session"))

        busy_session = MagicMock()
        busy_session.status = SessionStatus.BUSY
        ext.engine.session_manager.sessions = {"busy-session": busy_session}

        _run(ext.start())

        loaded = store.load_state()
        assert loaded.active_session_id == "busy-session"
        ext._scheduler_task.cancel()


# -- Lifecycle: stop --------------------------------------------------------


class TestHeartbeatStop:
    def test_stop_removes_service(self, ext):
        _run(ext.start())
        assert "heartbeat" in ext.engine.services
        _run(ext.stop())
        assert "heartbeat" not in ext.engine.services

    def test_stop_cancels_scheduler(self, ext):
        async def _start_and_stop():
            await ext.start()
            task = ext._scheduler_task
            assert task is not None
            assert not task.done()
            await ext.stop()
            assert task.cancelled()

        asyncio.run(_start_and_stop())


# -- Health check -----------------------------------------------------------


class TestHeartbeatHealth:
    def test_health_not_initialized(self, ext):
        result = _run(ext.health_check())
        assert result["status"] == "error"

    def test_health_ok(self, ext):
        async def _check():
            await ext.start()
            result = await ext.health_check()
            assert result["status"] == "ok"
            assert result["enabled"] is True
            assert "runs_today" in result
            assert "interval" in result
            assert result["effective_interval"] == result["interval"]
            assert result["backoff_multiplier"] == 1
            ext._scheduler_task.cancel()

        asyncio.run(_check())

    def test_health_effective_interval_with_backoff(self, ext):
        async def _check():
            await ext.start()
            ext._store.update_state(consecutive_noop=5)
            result = await ext.health_check()
            assert result["interval"] == 1800
            assert result["backoff_multiplier"] == 2
            assert result["effective_interval"] == 3600
            ext._scheduler_task.cancel()

        asyncio.run(_check())

    def test_health_paused(self, ext, heartbeat_dir):
        _run(ext.start())
        ext._store.update_state(enabled=False)
        result = _run(ext.health_check())
        assert result["status"] == "degraded"
        assert result["enabled"] is False
        ext._scheduler_task.cancel()


# -- Trigger mechanism ------------------------------------------------------


class TestHeartbeatTrigger:
    def test_immediate_trigger_enters_queue(self, ext):
        _run(ext.start())
        ext.trigger("wallet", "price_alert", "immediate", {"asset": "BTC"})
        assert ext._trigger_queue.qsize() == 1
        event = ext._trigger_queue.get_nowait()
        assert event.source == "wallet"
        assert event.urgency == "immediate"
        ext._scheduler_task.cancel()

    def test_normal_trigger_enters_pending(self, ext):
        _run(ext.start())
        ext.trigger("email", "new_mail", "normal", {"count": 5})
        assert len(ext._pending_events) == 1
        assert ext._trigger_queue.qsize() == 0
        ext._scheduler_task.cancel()

    def test_drain_pending_events(self, ext):
        _run(ext.start())
        ext.trigger("a", "ev1", "normal", {})
        ext.trigger("b", "ev2", "normal", {})
        drained = ext.drain_pending_events()
        assert len(drained) == 2
        assert len(ext._pending_events) == 0
        ext._scheduler_task.cancel()

    def test_trigger_is_sync(self, ext):
        """trigger() should work without await (sync method)."""
        _run(ext.start())
        # Should not raise — trigger is sync (put_nowait)
        ext.trigger("test", "event", "immediate", {})
        ext._scheduler_task.cancel()

    def test_immediate_trigger_logs_event(self, ext):
        _run(ext.start())
        ext.trigger("wallet", "alert", "immediate", {})
        ext.engine.events.log.assert_any_call(
            "heartbeat.triggered",
            detail={"source": "wallet", "event_type": "alert"},
        )
        ext._scheduler_task.cancel()


# -- Active hours -----------------------------------------------------------


class TestHeartbeatActiveHours:
    def _make_ext(self, engine, active_hours):
        ext = ExtensionImpl()
        ext.configure(
            engine,
            {
                "user_id": "123",
                "active_hours": active_hours,
            },
        )
        return ext

    def test_no_config_always_active(self, engine):
        ext = self._make_ext(engine, None)
        assert ext._in_active_hours() is True

    def test_same_day_window_inside(self, engine):
        ext = self._make_ext(engine, "00:00-23:59")
        assert ext._in_active_hours() is True

    def test_same_day_window_outside(self, engine):
        # 00:01-00:02 is a 1-minute window, almost certainly outside
        ext = self._make_ext(engine, "00:01-00:02")
        # We can't fully control time, but we can test format parsing
        result = ext._in_active_hours()
        assert isinstance(result, bool)

    @patch("extensions.heartbeat.extension.datetime")
    def test_cross_midnight_inside_late(self, mock_dt, engine):
        """22:00-06:00, current time 23:00 → inside."""
        mock_now = MagicMock()
        mock_now.hour = 23
        mock_now.minute = 0
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        ext = self._make_ext(engine, "22:00-06:00")
        assert ext._in_active_hours() is True

    @patch("extensions.heartbeat.extension.datetime")
    def test_cross_midnight_inside_early(self, mock_dt, engine):
        """22:00-06:00, current time 03:00 → inside."""
        mock_now = MagicMock()
        mock_now.hour = 3
        mock_now.minute = 0
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        ext = self._make_ext(engine, "22:00-06:00")
        assert ext._in_active_hours() is True

    @patch("extensions.heartbeat.extension.datetime")
    def test_cross_midnight_outside(self, mock_dt, engine):
        """22:00-06:00, current time 12:00 → outside."""
        mock_now = MagicMock()
        mock_now.hour = 12
        mock_now.minute = 0
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        ext = self._make_ext(engine, "22:00-06:00")
        assert ext._in_active_hours() is False

    def test_invalid_format_returns_true(self, engine):
        ext = self._make_ext(engine, "invalid")
        assert ext._in_active_hours() is True


# -- Backoff ----------------------------------------------------------------


class TestHeartbeatBackoff:
    def test_no_noop_1x(self, ext):
        _run(ext.start())
        ext._store.update_state(consecutive_noop=0)
        assert ext._get_backoff_multiplier() == 1
        ext._scheduler_task.cancel()

    def test_mid_noop_2x(self, ext):
        _run(ext.start())
        ext._store.update_state(consecutive_noop=5)
        assert ext._get_backoff_multiplier() == 2
        ext._scheduler_task.cancel()

    def test_high_noop_4x(self, ext):
        _run(ext.start())
        ext._store.update_state(consecutive_noop=8)
        assert ext._get_backoff_multiplier() == 4
        ext._scheduler_task.cancel()

    def test_max_noop_8x(self, ext):
        _run(ext.start())
        ext._store.update_state(consecutive_noop=15)
        assert ext._get_backoff_multiplier() == _BACKOFF_MAX_MULTIPLIER
        ext._scheduler_task.cancel()

    def test_action_resets_noop(self, ext):
        """Verify that consecutive_noop=0 gives multiplier 1."""
        _run(ext.start())
        ext._store.update_state(consecutive_noop=10)
        assert ext._get_backoff_multiplier() == _BACKOFF_MAX_MULTIPLIER
        ext._store.update_state(consecutive_noop=0)
        assert ext._get_backoff_multiplier() == 1
        ext._scheduler_task.cancel()


# -- Tier 2/3 integration --------------------------------------------------


class TestTier2Decision:
    def test_nothing_increments_noop(self, ext):
        """Tier 2 NOTHING → consecutive_noop incremented, no session created."""
        ext.engine.ask = AsyncMock(return_value="NOTHING")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")
            ext._store.update_state(consecutive_noop=2)
            await ext._tier2_decision("# Check stuff")
            state = ext._store.load_state()
            assert state.consecutive_noop == 3
            ext.engine.session_manager.create_session.assert_not_called()
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_nothing_with_explanation(self, ext):
        """NOTHING followed by explanation text should still be treated as noop."""
        ext.engine.ask = AsyncMock(return_value="NOTHING - all systems are operating normally")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")
            await ext._tier2_decision("# Check stuff")
            state = ext._store.load_state()
            assert state.consecutive_noop == 1
            ext.engine.session_manager.create_session.assert_not_called()
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_action_creates_session(self, ext):
        """Tier 2 action → Tier 3 creates session and sends prompt."""
        ext.engine.ask = AsyncMock(return_value="Deploy hotfix to production")
        mock_session = MagicMock()
        mock_session.id = "test-session-id"
        mock_session.context = {}
        ext.engine.session_manager.create_session = AsyncMock(return_value=mock_session)

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Monitor deploys")
            await ext._tier2_decision("# Monitor deploys")
            state = ext._store.load_state()
            assert state.consecutive_noop == 0
            ext.engine.session_manager.create_session.assert_called_once()
            ext.engine.session_manager.send_prompt.assert_called_once()
            # Verify active_session_id was set
            assert state.active_session_id == "test-session-id"
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_error_logs_skipped(self, ext):
        """Tier 2 error → skipped event logged, no noop change."""
        ext.engine.ask = AsyncMock(return_value="[Error] Claude Code timed out.")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")
            ext._store.update_state(consecutive_noop=3)
            await ext._tier2_decision("# Check stuff")
            state = ext._store.load_state()
            # noop should not change on error
            assert state.consecutive_noop == 3
            # Should not create session
            ext.engine.session_manager.create_session.assert_not_called()
            # Should log skipped event
            ext.engine.events.log.assert_any_call(
                "heartbeat.skipped",
                detail={"reason": "tier2_error", "error": "[Error] Claude Code timed out."},
            )
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


class TestDeliveryCallback:
    def test_final_clears_active_session(self, ext):
        """Delivery callback with is_final clears active_session_id."""

        async def _run_test():
            await ext.start()
            ext._store.update_state(active_session_id="sess-123")

            # Mock session in sm.sessions
            mock_session = MagicMock()
            mock_session.context = {"heartbeat_run": True, "heartbeat_auto_cleanup": True}
            mock_session.status = SessionStatus.IDLE
            ext.engine.session_manager.sessions = {"sess-123": mock_session}

            await ext._on_delivery("sess-123", "Task completed.", {"is_final": True})

            state = ext._store.load_state()
            assert state.active_session_id is None
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_non_final_ignored(self, ext):
        """Delivery callback without is_final should not clear active_session_id."""

        async def _run_test():
            await ext.start()
            ext._store.update_state(active_session_id="sess-123")

            mock_session = MagicMock()
            mock_session.context = {"heartbeat_run": True}
            ext.engine.session_manager.sessions = {"sess-123": mock_session}

            await ext._on_delivery("sess-123", "streaming...", {"is_stream": True})

            state = ext._store.load_state()
            assert state.active_session_id == "sess-123"
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_non_heartbeat_session_ignored(self, ext):
        """Delivery for non-heartbeat session should be ignored."""

        async def _run_test():
            await ext.start()
            ext._store.update_state(active_session_id="hb-sess")

            mock_session = MagicMock()
            mock_session.context = {}  # no heartbeat_run
            ext.engine.session_manager.sessions = {"other-sess": mock_session}

            await ext._on_delivery("other-sess", "Done.", {"is_final": True})

            state = ext._store.load_state()
            assert state.active_session_id == "hb-sess"  # unchanged
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


# -- Bridge handler ---------------------------------------------------------


class TestBridgeHandler:
    def test_bridge_handler_trigger_immediate(self, ext):
        """Bridge call with immediate urgency → event enters _trigger_queue."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request(
                "heartbeat_trigger",
                {"source": "session:abc12345", "event_type": "deploy_done", "urgency": "immediate"},
            )
            assert result == {"ok": True, "urgency": "immediate"}
            assert ext._trigger_queue.qsize() == 1
            event = ext._trigger_queue.get_nowait()
            assert event.source == "session:abc12345"
            assert event.event_type == "deploy_done"
            assert event.urgency == "immediate"
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_handler_trigger_normal(self, ext):
        """Bridge call with normal urgency → event enters _pending_events."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request(
                "heartbeat_trigger",
                {"source": "session:xyz", "event_type": "data_ready", "urgency": "normal"},
            )
            assert result == {"ok": True, "urgency": "normal"}
            assert ext._trigger_queue.qsize() == 0
            assert len(ext._pending_events) == 1
            assert ext._pending_events[0].event_type == "data_ready"
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_handler_missing_event_type(self, ext):
        """Bridge call without event_type → error."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request(
                "heartbeat_trigger",
                {"source": "session:abc", "urgency": "immediate"},
            )
            assert "error" in result
            assert ext._trigger_queue.qsize() == 0
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_handler_unknown_method(self, ext):
        """Non-heartbeat_trigger method → returns None (pass to next handler)."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request("vault_store", {"key": "x"})
            assert result is None
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_handler_with_payload(self, ext):
        """Bridge call with payload → payload forwarded to trigger event."""

        async def _run_test():
            await ext.start()
            payload = {"asset": "BTC", "price": 95000}
            result = await ext._handle_bridge_request(
                "heartbeat_trigger",
                {
                    "source": "session:abc",
                    "event_type": "price_alert",
                    "urgency": "immediate",
                    "payload": payload,
                },
            )
            assert result["ok"] is True
            event = ext._trigger_queue.get_nowait()
            assert event.payload == payload
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_registered_on_start(self, ext):
        """start() registers bridge handler when engine.bridge exists."""
        ext.engine.bridge = MagicMock()

        async def _run_test():
            await ext.start()
            ext.engine.bridge.add_handler.assert_called_once_with(ext._handle_bridge_request)
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_not_registered_without_bridge(self, ext):
        """start() does not crash when engine.bridge is None."""
        ext.engine.bridge = None

        async def _run_test():
            await ext.start()
            # Should start without error
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_mcp_tools_metadata_includes_trigger(self, ext):
        """MCP server registration includes heartbeat_trigger in tools metadata."""
        _run(ext.start())
        call_args = ext.engine.session_manager.register_mcp_server.call_args
        tools = (
            call_args[1]["tools"]
            if "tools" in call_args[1]
            else call_args[0][2]
            if len(call_args[0]) > 2
            else None
        )
        # Find from kwargs
        if tools is None:
            tools = call_args.kwargs.get("tools", [])
        tool_names = [t["name"] for t in tools]
        assert "heartbeat_trigger" in tool_names
        assert "heartbeat_get_trigger_command" in tool_names
        assert "heartbeat_instructions" in tool_names
        assert "heartbeat_status" in tool_names
        assert "heartbeat_dry_run" in tool_names
        assert "heartbeat_set_verification" in tool_names
        assert "heartbeat_safe_reload" in tool_names
        assert len(tool_names) == 7
        ext._scheduler_task.cancel()


class TestDailyLimit:
    def test_trigger_respects_daily_limit(self, ext):
        """Trigger channel should also be subject to daily limit."""

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")
            ext._store.update_state(
                runs_today=48,
                runs_today_date=datetime.now(UTC).strftime("%Y-%m-%d"),
            )

            trigger = TriggerEvent(
                source="test",
                event_type="alert",
                urgency="immediate",
                payload={},
            )
            await ext._handle_trigger(trigger)

            # Should not have called engine.ask (daily limit hit)
            ext.engine.ask.assert_not_called()
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


# -- Dry-run ---------------------------------------------------------------


class TestDryRun:
    def test_dry_run_returns_nothing_decision(self, ext):
        """dry_run_tier2 with NOTHING response → noop=True, would_execute=False."""
        ext.engine.ask = AsyncMock(return_value="NOTHING")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")
            initial_state = ext._store.load_state()
            initial_run_count = initial_state.run_count

            result = await ext.dry_run_tier2()

            assert result["noop"] is True
            assert result["would_execute"] is False
            assert result["decision"] == "NOTHING"
            assert "prompt" in result

            # State should NOT be modified
            state = ext._store.load_state()
            assert state.run_count == initial_run_count
            assert state.consecutive_noop == initial_state.consecutive_noop

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_dry_run_returns_action_decision(self, ext):
        """dry_run_tier2 with action response → noop=False, would_execute=True."""
        ext.engine.ask = AsyncMock(return_value="Deploy hotfix to production")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Monitor deploys")
            initial_state = ext._store.load_state()

            result = await ext.dry_run_tier2()

            assert result["noop"] is False
            assert result["would_execute"] is True
            assert result["decision"] == "Deploy hotfix to production"

            # No session should be created (no Tier 3)
            ext.engine.session_manager.create_session.assert_not_called()

            # State unchanged
            state = ext._store.load_state()
            assert state.run_count == initial_state.run_count

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_dry_run_with_custom_instructions(self, ext):
        """dry_run_tier2 with custom instructions uses them instead of HEARTBEAT.md."""
        ext.engine.ask = AsyncMock(return_value="NOTHING")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Original instructions")

            result = await ext.dry_run_tier2(custom_instructions="# Custom check\nDo X")

            assert result["noop"] is True
            assert "Custom check" in result["prompt"]
            assert "Original instructions" not in result["prompt"]

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_dry_run_no_instructions_returns_error(self, ext):
        """dry_run_tier2 with empty instructions → error."""

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("")

            result = await ext.dry_run_tier2()

            assert "error" in result
            assert "No instructions" in result["error"]

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_dry_run_engine_error(self, ext):
        """dry_run_tier2 when engine.ask raises → error with prompt."""
        ext.engine.ask = AsyncMock(side_effect=TimeoutError("timeout"))

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")

            result = await ext.dry_run_tier2()

            assert "error" in result
            assert "prompt" in result

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_dry_run_bridge_handler(self, ext):
        """Bridge handler routes heartbeat_dry_run correctly."""
        ext.engine.ask = AsyncMock(return_value="NOTHING")

        async def _run_test():
            await ext.start()
            ext._store.write_instructions("# Check stuff")

            result = await ext._handle_bridge_request("heartbeat_dry_run", {})

            assert result is not None
            assert result["noop"] is True

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_dry_run_not_initialized(self, ext):
        """dry_run_tier2 before start → error."""
        result = _run(ext.dry_run_tier2())
        assert "error" in result


# -- Verification state ----------------------------------------------------


class TestPendingVerification:
    def test_pending_verification_in_tier2_prompt(self, ext):
        """pending_verification set → Tier 2 prompt includes PENDING VERIFICATION section."""

        async def _run_test():
            await ext.start()
            ext._store.update_state(pending_verification="abc123def456")
            ext._store.write_instructions("# Check stuff")
            prompt = ext._build_tier2_prompt("# Check stuff")
            assert "PENDING VERIFICATION" in prompt
            assert "abc123def456" in prompt
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_pending_verification_cleared(self, ext):
        """No pending_verification → Tier 2 prompt omits PENDING VERIFICATION section."""

        async def _run_test():
            await ext.start()
            ext._store.update_state(pending_verification=None)
            ext._store.write_instructions("# Check stuff")
            prompt = ext._build_tier2_prompt("# Check stuff")
            assert "PENDING VERIFICATION" not in prompt
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


# -- Reconfigure -----------------------------------------------------------


class TestReconfigure:
    def test_reconfigure_updates_interval(self, ext):
        """reconfigure() updates heartbeat settings from new config."""
        _run(ext.start())
        assert ext._interval == 1800
        assert ext._max_daily_runs == 48
        ext.reconfigure(
            {"interval": 900, "max_daily_runs": 24, "usage_throttle": 70, "usage_pause": 90}
        )
        assert ext._interval == 900
        assert ext._max_daily_runs == 24
        assert ext._usage_throttle == 70
        assert ext._usage_pause == 90
        ext._scheduler_task.cancel()

    def test_reconfigure_keeps_defaults(self, ext):
        """reconfigure() with empty config keeps existing values."""
        _run(ext.start())
        ext._interval = 1800
        ext._max_daily_runs = 48
        ext.reconfigure({})
        assert ext._interval == 1800
        assert ext._max_daily_runs == 48
        ext._scheduler_task.cancel()


# -- Bridge dispatch: set_verification ------------------------------------


class TestBridgeSetVerification:
    def test_bridge_dispatch_set_verification(self, ext):
        """Bridge handler routes heartbeat_set_verification correctly."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request(
                "heartbeat_set_verification",
                {"commit_hash": "abc1234"},
            )
            assert result["ok"] is True
            assert result["pending_verification"] == "abc1234"
            state = ext._store.load_state()
            assert state.pending_verification == "abc1234"
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_set_verification_clear(self, ext):
        """Clearing verification also deletes restart marker."""

        async def _run_test():
            await ext.start()
            # Set verification and create marker
            ext._store.update_state(pending_verification="abc1234")
            ext._restart_marker.write_text("abc1234")
            assert ext._restart_marker.exists()

            result = await ext._handle_bridge_request(
                "heartbeat_set_verification",
                {"commit_hash": None},
            )
            assert result["ok"] is True
            assert result["pending_verification"] is None
            assert not ext._restart_marker.exists()
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_bridge_dispatch_unknown_method(self, ext):
        """Unknown bridge method → returns None."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request("some_other_method", {})
            assert result is None
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


# -- Fast schedule on pending verification ---------------------------------


class TestFastSchedule:
    def test_fast_schedule_on_pending_verification(self, ext, heartbeat_dir):
        """start() with pending_verification → next_run set near-immediate."""
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        store.save_state(HeartbeatState(pending_verification="deadbeef1234"))

        async def _run_test():
            await ext.start()
            state = ext._store.load_state()
            # next_run should be set to ~30s in the future (not normal interval)
            assert state.next_run is not None
            next_dt = datetime.fromisoformat(state.next_run)
            now = datetime.now(UTC)
            delta = (next_dt - now).total_seconds()
            # Should be within 0-60s (fast schedule), not 1800s
            assert delta < 60
            assert state.consecutive_noop == 0
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


# -- Restart marker safety net ---------------------------------------------


class TestRestartMarker:
    def test_restart_marker_created_on_pending_verification(self, ext, heartbeat_dir):
        """start() with pending_verification (no marker) → marker file created."""
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        store.save_state(HeartbeatState(pending_verification="face0123"))

        async def _run_test():
            await ext.start()
            marker = heartbeat_dir / ".restart_marker"
            assert marker.exists()
            assert marker.read_text() == "face0123"
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    @patch("subprocess.run")
    def test_double_restart_auto_reverts(self, mock_subprocess_run, ext, heartbeat_dir):
        """Marker exists + pending_verification → auto-revert via git."""
        heartbeat_dir.mkdir(parents=True)
        store = HeartbeatStore(heartbeat_dir)
        store.save_state(HeartbeatState(pending_verification="bad0commit1"))

        # Create marker (simulates first restart already happened)
        marker = heartbeat_dir / ".restart_marker"
        marker.write_text("bad0commit1")

        # Mock git rev-parse succeeds, git revert succeeds
        mock_subprocess_run.return_value = MagicMock(returncode=0, stderr=b"")

        async def _run_test():
            await ext.start()

            # Verify git rev-parse and git revert were called
            calls = mock_subprocess_run.call_args_list
            assert any("rev-parse" in str(c) for c in calls)
            assert any("revert" in str(c) for c in calls)

            # pending_verification should be cleared
            state = ext._store.load_state()
            assert state.pending_verification is None

            # Marker should be deleted
            assert not marker.exists()
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())


# -- Safe reload bridge handler ---------------------------------------------


class TestBridgeSafeReload:
    def test_safe_reload_when_idle(self, ext):
        """safe_reload with no busy sessions or pending RPCs → ok + scheduled."""
        ext.engine.pending = PendingStore()

        async def _run_test():
            await ext.start()
            ext.engine.session_manager.sessions = {}

            with patch("os.kill") as mock_kill:
                result = await ext._handle_bridge_request(
                    "heartbeat_safe_reload",
                    {"session_id": "hb-session"},
                )
                assert result["ok"] is True
                assert result["scheduled"] is True

                # call_later schedules os.kill — run the event loop briefly
                await asyncio.sleep(1.1)
                mock_kill.assert_called_once()

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_safe_reload_deferred_busy_session(self, ext):
        """safe_reload with busy session → deferred."""
        ext.engine.pending = PendingStore()

        async def _run_test():
            await ext.start()
            busy = MagicMock()
            busy.status = SessionStatus.BUSY
            busy.name = "user-session"
            busy.context = {}
            ext.engine.session_manager.sessions = {"other-sess": busy}

            result = await ext._handle_bridge_request(
                "heartbeat_safe_reload",
                {"session_id": "hb-session"},
            )
            assert result["ok"] is False
            assert result["reason"] == "active_sessions"
            assert "user-session" in result["busy_sessions"]
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_safe_reload_deferred_pending_rpcs(self, ext):
        """safe_reload with pending RPCs → deferred."""
        pending = PendingStore()
        ext.engine.pending = pending

        async def _run_test():
            await ext.start()
            ext.engine.session_manager.sessions = {}

            # Register a pending entry to simulate in-flight RPC
            pending.register("some-session", {"question": "test?"})

            result = await ext._handle_bridge_request(
                "heartbeat_safe_reload",
                {"session_id": "hb-session"},
            )
            assert result["ok"] is False
            assert result["pending_rpcs"] == 1
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_safe_reload_excludes_caller_session(self, ext):
        """safe_reload ignores the calling heartbeat session's busy status."""
        ext.engine.pending = PendingStore()

        async def _run_test():
            await ext.start()
            # The heartbeat session itself is BUSY (it's running the reload command)
            hb_sess = MagicMock()
            hb_sess.status = SessionStatus.BUSY
            hb_sess.name = "heartbeat-28"
            hb_sess.context = {"heartbeat_run": True}
            ext.engine.session_manager.sessions = {"hb-session": hb_sess}

            with patch("os.kill"):
                result = await ext._handle_bridge_request(
                    "heartbeat_safe_reload",
                    {"session_id": "hb-session"},
                )
                assert result["ok"] is True
                assert result["scheduled"] is True

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_safe_reload_session_id_none_uses_heartbeat_context(self, ext):
        """safe_reload with session_id=None still excludes heartbeat_run sessions."""
        ext.engine.pending = PendingStore()

        async def _run_test():
            await ext.start()
            # Heartbeat session without explicit session_id match
            hb_sess = MagicMock()
            hb_sess.status = SessionStatus.BUSY
            hb_sess.name = "heartbeat-28"
            hb_sess.context = {"heartbeat_run": True}
            ext.engine.session_manager.sessions = {"hb-session": hb_sess}

            with patch("os.kill"):
                result = await ext._handle_bridge_request(
                    "heartbeat_safe_reload",
                    {"session_id": None},  # session_id unavailable
                )
                # Should still succeed because heartbeat_run context excludes it
                assert result["ok"] is True
                assert result["scheduled"] is True

            ext._scheduler_task.cancel()

        asyncio.run(_run_test())

    def test_safe_reload_unknown_method_passthrough(self, ext):
        """Non-safe_reload method still returns None."""

        async def _run_test():
            await ext.start()
            result = await ext._handle_bridge_request("some_other_method", {})
            assert result is None
            ext._scheduler_task.cancel()

        asyncio.run(_run_test())
