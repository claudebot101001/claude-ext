"""Tests for per-session customization hooks."""

import asyncio
import json

import pytest

from core.session import Session, SessionManager, SessionOverrides
from core.session_context import get_extension_state, register_legacy_keys


@pytest.fixture
def sm(tmp_path):
    return SessionManager(
        base_dir=tmp_path,
        engine_config={"permission_mode": "bypassPermissions"},
    )


def _make_session(slot=1, user_id="u1", **kwargs):
    return Session(
        id="test-sid",
        name="test",
        slot=slot,
        user_id=user_id,
        working_dir="/tmp",
        **kwargs,
    )


class TestUserIdInjection:
    def test_user_id_injected_in_mcp_env(self, sm):
        sm.register_mcp_server("test", {"command": "python", "args": ["s.py"]})
        session = _make_session(user_id="12345")
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        config = sm._generate_mcp_config(session, sdir)
        assert config is not None
        env = config["mcpServers"]["test"]["env"]
        assert env["CLAUDE_EXT_USER_ID"] == "12345"


class TestNoCustomizers:
    def test_no_customizers_returns_defaults(self, sm):
        session = _make_session()
        overrides = sm._collect_overrides(session)
        assert overrides.extra_system_prompt is None
        assert overrides.exclude_mcp_servers is None
        assert overrides.extra_mcp_servers is None
        assert overrides.extra_disallowed_tools is None
        assert overrides.extra_env_unset is None


class TestSingleCustomizer:
    def test_single_customizer_extra_prompt(self, sm):
        def customizer(session):
            return SessionOverrides(extra_system_prompt=["You are a helper."])

        sm.add_session_customizer(customizer)
        session = _make_session()
        overrides = sm._collect_overrides(session)
        assert overrides.extra_system_prompt == ["You are a helper."]


class TestMultipleCustomizers:
    def test_multiple_customizers_merge(self, sm):
        def c1(session):
            return SessionOverrides(
                extra_system_prompt=["prompt1"],
                extra_disallowed_tools=["ToolA"],
                exclude_mcp_servers={"server_x"},
                extra_mcp_servers={"s1": {"command": "python"}},
                extra_env_unset=["VAR1"],
            )

        def c2(session):
            return SessionOverrides(
                extra_system_prompt=["prompt2"],
                extra_disallowed_tools=["ToolB"],
                exclude_mcp_servers={"server_y"},
                extra_mcp_servers={"s2": {"command": "node"}},
                extra_env_unset=["VAR2"],
            )

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        session = _make_session()
        overrides = sm._collect_overrides(session)

        assert overrides.extra_system_prompt == ["prompt1", "prompt2"]
        assert overrides.extra_disallowed_tools == ["ToolA", "ToolB"]
        assert overrides.exclude_mcp_servers == {"server_x", "server_y"}
        assert overrides.extra_mcp_servers == {
            "s1": {"command": "python"},
            "s2": {"command": "node"},
        }
        assert overrides.extra_env_unset == ["VAR1", "VAR2"]


class TestCustomizerEdgeCases:
    def test_customizer_exception_skipped(self, sm):
        def bad(session):
            raise RuntimeError("boom")

        def good(session):
            return SessionOverrides(extra_system_prompt=["still works"])

        sm.add_session_customizer(bad)
        sm.add_session_customizer(good)
        session = _make_session()
        overrides = sm._collect_overrides(session)
        assert overrides.extra_system_prompt == ["still works"]

    def test_customizer_returns_none_skipped(self, sm):
        def skip(session):
            return None

        def provide(session):
            return SessionOverrides(extra_disallowed_tools=["X"])

        sm.add_session_customizer(skip)
        sm.add_session_customizer(provide)
        session = _make_session()
        overrides = sm._collect_overrides(session)
        assert overrides.extra_disallowed_tools == ["X"]


class TestMCPConfigOverrides:
    def test_exclude_mcp_servers(self, sm):
        sm.register_mcp_server("keep", {"command": "python"})
        sm.register_mcp_server("remove", {"command": "python"})

        def customizer(session):
            return SessionOverrides(exclude_mcp_servers={"remove"})

        sm.add_session_customizer(customizer)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = sm._collect_overrides(session)
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        assert "keep" in config["mcpServers"]
        assert "remove" not in config["mcpServers"]

    def test_extra_mcp_servers(self, sm):
        sm.register_mcp_server("global", {"command": "python"})

        def customizer(session):
            return SessionOverrides(
                extra_mcp_servers={"extra": {"command": "node", "args": ["s.js"]}}
            )

        sm.add_session_customizer(customizer)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = sm._collect_overrides(session)
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        assert "global" in config["mcpServers"]
        assert "extra" in config["mcpServers"]
        # Extra servers also get session-specific env vars
        assert "CLAUDE_EXT_SESSION_ID" in config["mcpServers"]["extra"]["env"]

    def test_exclude_only_affects_global_servers(self, sm):
        """Exclude removes from global registry, not from extra_mcp_servers."""
        sm.register_mcp_server("global_a", {"command": "python"})

        def c1(session):
            return SessionOverrides(exclude_mcp_servers={"global_a", "extra_b"})

        def c2(session):
            return SessionOverrides(extra_mcp_servers={"extra_b": {"command": "node"}})

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = sm._collect_overrides(session)
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        # global_a excluded
        assert "global_a" not in config["mcpServers"]
        # extra_b survives because exclude only affects global registry (R1)
        assert "extra_b" in config["mcpServers"]


class TestCustomizerPerPrompt:
    def test_customizer_called_per_prompt(self, sm):
        call_count = 0

        def counting(session):
            nonlocal call_count
            call_count += 1
            return None

        sm.add_session_customizer(counting)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        # _generate_run_scripts calls _collect_overrides internally
        sm._generate_run_scripts(session, sdir, is_first=True)
        sm._generate_run_scripts(session, sdir, is_first=False)
        assert call_count == 2


class TestPrePromptHooks:
    def test_hooks_run_in_registration_order(self, sm):
        calls = []

        async def h1(session):
            calls.append(("h1", session.id))

        async def h2(session):
            calls.append(("h2", session.id))

        sm.add_pre_prompt_hook(h1)
        sm.add_pre_prompt_hook(h2)
        session = _make_session()

        asyncio.run(sm._run_pre_prompt_hooks(session))

        assert calls == [("h1", "test-sid"), ("h2", "test-sid")]

    def test_hook_exception_is_skipped(self, sm):
        calls = []

        async def bad(session):
            raise RuntimeError("boom")

        async def good(session):
            calls.append(session.id)

        sm.add_pre_prompt_hook(bad)
        sm.add_pre_prompt_hook(good)
        session = _make_session()

        asyncio.run(sm._run_pre_prompt_hooks(session))

        assert calls == ["test-sid"]


class TestContextNormalization:
    def test_normalize_session_contexts_migrates_registered_keys(self, sm):
        register_legacy_keys({"__test_session_flag__": ("test_ns", "flag")})
        session = Session(
            id="sess-ctx",
            name="legacy",
            slot=1,
            user_id="u1",
            working_dir="/tmp",
            context={"__test_session_flag__": True},
        )
        sm.sessions[session.id] = session

        changed = sm.normalize_session_contexts()

        assert changed == 1
        assert get_extension_state(session, "test_ns", "flag") is True
        state = json.loads((sm.session_dir(session.id) / "state.json").read_text(encoding="utf-8"))
        assert "__test_session_flag__" not in state["context"]
        assert state["context"]["_extensions"]["test_ns"]["flag"] is True


class TestPendingDeliveryFlush:
    def test_flush_can_be_deferred_until_all_callbacks_registered(self, sm):
        calls = []

        async def cb1(session_id, text, metadata):
            calls.append(("cb1", session_id, text, metadata.get("is_final")))

        async def cb2(session_id, text, metadata):
            calls.append(("cb2", session_id, text, metadata.get("is_final")))

        async def run():
            sm._pending_deliveries.append(("sess-1", "done", {"is_final": True}))
            sm.set_delivery_flush_enabled(False)
            sm.add_delivery_callback(cb1)
            sm.add_delivery_callback(cb2)

            assert calls == []
            assert sm.flush_pending_deliveries() == 1
            await asyncio.sleep(0)

        asyncio.run(run())

        assert calls == [
            ("cb1", "sess-1", "done", True),
            ("cb2", "sess-1", "done", True),
        ]
        assert sm._pending_deliveries == []
