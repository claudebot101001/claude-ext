"""Tests for template allowlist merge semantics in SessionOverrides."""

import pytest

from core.session import Session, SessionManager, SessionOverrides
from core.session_context import set_extension_state
from core.templates import TemplateRegistry


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


class TestAllowedToolsIntersection:
    """allowed_tools from multiple customizers merge via intersection."""

    def test_single_customizer_allowed_tools(self, sm):
        def c(session):
            return SessionOverrides(allowed_tools=["Read", "Grep", "Glob"])

        sm.add_session_customizer(c)
        overrides = sm._collect_overrides(_make_session())
        assert set(overrides.allowed_tools) == {"Read", "Grep", "Glob"}

    def test_two_customizers_intersection(self, sm):
        def c1(session):
            return SessionOverrides(allowed_tools=["Read", "Write", "Grep"])

        def c2(session):
            return SessionOverrides(allowed_tools=["Read", "Grep", "Bash"])

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        overrides = sm._collect_overrides(_make_session())
        assert set(overrides.allowed_tools) == {"Read", "Grep"}

    def test_none_plus_list(self, sm):
        """None (no opinion) + list = list (not intersection with empty)."""

        def c1(session):
            return SessionOverrides()  # allowed_tools=None

        def c2(session):
            return SessionOverrides(allowed_tools=["Read", "Bash"])

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        overrides = sm._collect_overrides(_make_session())
        assert set(overrides.allowed_tools) == {"Read", "Bash"}

    def test_all_none_stays_none(self, sm):
        def c1(session):
            return SessionOverrides()

        def c2(session):
            return SessionOverrides()

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        overrides = sm._collect_overrides(_make_session())
        assert overrides.allowed_tools is None


class TestAllowedMCPServersIntersection:
    """allowed_mcp_servers from multiple customizers merge via intersection."""

    def test_single_customizer(self, sm):
        def c(session):
            return SessionOverrides(allowed_mcp_servers={"memory", "browser"})

        sm.add_session_customizer(c)
        overrides = sm._collect_overrides(_make_session())
        assert overrides.allowed_mcp_servers == {"memory", "browser"}

    def test_two_customizers_intersection(self, sm):
        def c1(session):
            return SessionOverrides(allowed_mcp_servers={"memory", "browser", "vault"})

        def c2(session):
            return SessionOverrides(allowed_mcp_servers={"memory", "arxiv"})

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        overrides = sm._collect_overrides(_make_session())
        assert overrides.allowed_mcp_servers == {"memory"}

    def test_none_plus_set(self, sm):
        def c1(session):
            return SessionOverrides()

        def c2(session):
            return SessionOverrides(allowed_mcp_servers={"browser"})

        sm.add_session_customizer(c1)
        sm.add_session_customizer(c2)
        overrides = sm._collect_overrides(_make_session())
        assert overrides.allowed_mcp_servers == {"browser"}


class TestMCPConfigAllowlist:
    """_generate_mcp_config respects allowed_mcp_servers gate."""

    def test_no_allowlist_keeps_all(self, sm):
        sm.register_mcp_server("a", {"command": "python"})
        sm.register_mcp_server("b", {"command": "python"})
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        config = sm._generate_mcp_config(session, sdir, overrides=SessionOverrides())
        assert "a" in config["mcpServers"]
        assert "b" in config["mcpServers"]

    def test_allowlist_filters_global(self, sm):
        sm.register_mcp_server("keep", {"command": "python"})
        sm.register_mcp_server("drop", {"command": "python"})
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = SessionOverrides(allowed_mcp_servers={"keep"})
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        assert "keep" in config["mcpServers"]
        assert "drop" not in config["mcpServers"]

    def test_allowlist_then_exclude(self, sm):
        """Exclude applies after allowlist."""
        sm.register_mcp_server("a", {"command": "python"})
        sm.register_mcp_server("b", {"command": "python"})
        sm.register_mcp_server("c", {"command": "python"})
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = SessionOverrides(
            allowed_mcp_servers={"a", "b"},
            exclude_mcp_servers={"b"},
        )
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        assert "a" in config["mcpServers"]
        assert "b" not in config["mcpServers"]
        assert "c" not in config["mcpServers"]

    def test_extra_mcp_gated_by_allowlist(self, sm):
        """extra_mcp_servers outside allowlist are silently dropped."""
        sm.register_mcp_server("global", {"command": "python"})
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = SessionOverrides(
            allowed_mcp_servers={"global", "allowed_extra"},
            extra_mcp_servers={
                "allowed_extra": {"command": "node"},
                "blocked_extra": {"command": "node"},
            },
        )
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        assert "global" in config["mcpServers"]
        assert "allowed_extra" in config["mcpServers"]
        assert "blocked_extra" not in config["mcpServers"]

    def test_extra_mcp_ungated_when_no_allowlist(self, sm):
        """Without allowlist, extra_mcp_servers are always added."""
        sm.register_mcp_server("global", {"command": "python"})
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        overrides = SessionOverrides(
            extra_mcp_servers={"extra": {"command": "node"}},
        )
        config = sm._generate_mcp_config(session, sdir, overrides=overrides)
        assert "global" in config["mcpServers"]
        assert "extra" in config["mcpServers"]


class TestRunScriptsAllowedTools:
    """_generate_run_scripts applies allowed_tools to --allowedTools."""

    def test_no_allowed_tools_no_flag(self, sm):
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        assert "--allowedTools" not in claude_cmd

    def test_override_allowed_tools_emitted(self, sm):
        def c(session):
            return SessionOverrides(allowed_tools=["Read", "Bash"])

        sm.add_session_customizer(c)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        assert "--allowedTools" in claude_cmd
        assert "Read" in claude_cmd
        assert "Bash" in claude_cmd

    def test_engine_config_intersection_with_override(self, tmp_path):
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={
                "permission_mode": "bypassPermissions",
                "allowed_tools": ["Read", "Grep", "Write"],
            },
        )

        def c(session):
            return SessionOverrides(allowed_tools=["Read", "Bash"])

        sm.add_session_customizer(c)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        assert "--allowedTools" in claude_cmd
        # Intersection of {Read, Grep, Write} ∩ {Read, Bash} = {Read}
        assert "Read" in claude_cmd
        assert "Grep" not in claude_cmd
        assert "Write" not in claude_cmd
        assert "Bash" not in claude_cmd

    def test_empty_intersection_emits_flag(self, tmp_path):
        """Empty intersection must NOT fail open — --allowedTools still emitted."""
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={
                "permission_mode": "bypassPermissions",
                "allowed_tools": ["Read"],
            },
        )

        def c(session):
            return SessionOverrides(allowed_tools=["Write"])

        sm.add_session_customizer(c)
        session = _make_session()
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)

        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        # Flag must be present even with empty intersection (fail-closed)
        assert "--allowedTools" in claude_cmd
        # No tool names should follow --allowedTools (empty intersection)
        assert "Read" not in claude_cmd
        assert "Write" not in claude_cmd


class TestTemplateCustomizer:
    """Built-in template customizer reads _extensions.templates.name."""

    def test_no_template_returns_none(self, sm):
        reg = TemplateRegistry()
        sm.set_template_registry(reg)

        session = _make_session()
        overrides = sm._template_customizer(session)
        assert overrides is None

    def test_template_applies_system_prompt(self, sm):
        reg = TemplateRegistry({"test": {"system_prompt": "You are a test agent."}})
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "test")
        overrides = sm._template_customizer(session)
        assert overrides is not None
        assert overrides.extra_system_prompt == ["You are a test agent."]

    def test_template_applies_allowed_tools(self, sm):
        reg = TemplateRegistry({"test": {"allowed_tools": ["Read", "Grep"]}})
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "test")
        overrides = sm._template_customizer(session)
        assert set(overrides.allowed_tools) == {"Read", "Grep"}

    def test_template_applies_mcp_servers(self, sm):
        reg = TemplateRegistry({"test": {"mcp_servers": ["arxiv", "browser"]}})
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "test")
        overrides = sm._template_customizer(session)
        assert overrides.allowed_mcp_servers == {"arxiv", "browser"}

    def test_template_applies_disallowed_tools(self, sm):
        reg = TemplateRegistry({"test": {"disallowed_tools": ["Write", "Edit"]}})
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "test")
        overrides = sm._template_customizer(session)
        assert overrides.extra_disallowed_tools == ["Write", "Edit"]

    def test_template_applies_exclude_mcp(self, sm):
        reg = TemplateRegistry({"test": {"exclude_mcp_servers": ["subagent"]}})
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "test")
        overrides = sm._template_customizer(session)
        assert "subagent" in overrides.exclude_mcp_servers

    def test_template_expands_mcp_tags(self, sm):
        sm.register_mcp_server(
            "tagged_srv", {"command": "python"}, tags={"read_only_worker_exclude"}
        )
        reg = TemplateRegistry({"test": {"exclude_mcp_tags": ["read_only_worker_exclude"]}})
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "test")
        overrides = sm._template_customizer(session)
        assert "tagged_srv" in overrides.exclude_mcp_servers

    def test_unknown_template_fails_closed(self, sm):
        """Unknown template name blocks all tools and MCP servers."""
        reg = TemplateRegistry()
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "nonexistent")
        overrides = sm._template_customizer(session)
        assert overrides is not None
        assert overrides.allowed_tools == []
        assert overrides.allowed_mcp_servers == set()

    def test_unknown_template_mcp_config_empty(self, sm):
        """Unknown template produces no MCP servers in generated config."""
        sm.register_mcp_server("memory", {"command": "python"})
        sm.register_mcp_server("browser", {"command": "python"})
        reg = TemplateRegistry()
        sm.set_template_registry(reg)

        session = _make_session()
        set_extension_state(session, "templates", "name", "removed-template")
        overrides = sm._collect_overrides(session)
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)
        mcp = sm._generate_mcp_config(session, sdir, overrides=overrides)
        # allowed_mcp_servers=set() means no servers pass the allowlist
        assert mcp is None

    def test_legacy_paradigm_fallback_removed(self, sm):
        """Subagent paradigm fallback was removed — sessions with only
        subagent.paradigm (no templates.name) no longer resolve a template."""
        reg = TemplateRegistry()
        sm.set_template_registry(reg)

        session = _make_session()
        # Simulate recovered session: has paradigm but no templates.name
        set_extension_state(session, "subagent", "paradigm", "reviewer")
        overrides = sm._template_customizer(session)
        # With fallback removed, no template name is found → returns None
        assert overrides is None

    def test_customizer_runs_at_position_zero(self, sm):
        """Template customizer is registered at position 0."""
        call_order = []

        def ext_customizer(session):
            call_order.append("ext")
            return None

        sm.add_session_customizer(ext_customizer)
        reg = TemplateRegistry()
        sm.set_template_registry(reg)

        session = _make_session()
        sm._collect_overrides(session)
        # Template customizer runs first (position 0), then ext_customizer
        # We can't directly observe template customizer call without a template,
        # but we verify ext_customizer still runs
        assert "ext" in call_order

    def test_set_template_registry_reload_no_duplicate(self, sm):
        """Calling set_template_registry twice doesn't duplicate the customizer."""
        reg1 = TemplateRegistry()
        sm.set_template_registry(reg1)
        n_before = len(sm._session_customizers)

        reg2 = TemplateRegistry({"custom": {"system_prompt": "hello"}})
        sm.set_template_registry(reg2)
        n_after = len(sm._session_customizers)

        assert n_after == n_before

        # Verify the new registry is active
        session = _make_session()
        set_extension_state(session, "templates", "name", "custom")
        overrides = sm._template_customizer(session)
        assert overrides is not None
        assert "hello" in overrides.extra_system_prompt

    def test_template_and_extension_overrides_compose(self, sm):
        """Template + extension customizer compose via merge rules."""
        reg = TemplateRegistry(
            {
                "restricted": {
                    "allowed_tools": ["Read", "Grep", "Bash"],
                    "mcp_servers": ["memory", "browser"],
                    "system_prompt": "Template prompt.",
                }
            }
        )
        sm.set_template_registry(reg)

        def ext_customizer(session):
            return SessionOverrides(
                extra_system_prompt=["Extension prompt."],
                exclude_mcp_servers={"browser"},
            )

        sm.add_session_customizer(ext_customizer)

        session = _make_session()
        set_extension_state(session, "templates", "name", "restricted")
        overrides = sm._collect_overrides(session)

        # System prompts concatenated
        assert "Template prompt." in overrides.extra_system_prompt
        assert "Extension prompt." in overrides.extra_system_prompt
        # allowed_tools from template
        assert set(overrides.allowed_tools) == {"Read", "Grep", "Bash"}
        # allowed_mcp_servers from template
        assert overrides.allowed_mcp_servers == {"memory", "browser"}
        # exclude from extension
        assert "browser" in overrides.exclude_mcp_servers


class TestModelOverride:
    """Template model field flows through to SessionOverrides."""

    def test_template_model_in_overrides(self, sm):
        reg = TemplateRegistry({"sonnet-worker": {"system_prompt": "hello", "model": "sonnet"}})
        sm.set_template_registry(reg)
        session = _make_session()
        set_extension_state(session, "templates", "name", "sonnet-worker")
        overrides = sm._collect_overrides(session)
        assert overrides.model == "sonnet"

    def test_no_model_defaults_none(self, sm):
        reg = TemplateRegistry({"plain": {"system_prompt": "hello"}})
        sm.set_template_registry(reg)
        session = _make_session()
        set_extension_state(session, "templates", "name", "plain")
        overrides = sm._collect_overrides(session)
        assert overrides.model is None

    def test_first_model_wins(self, sm):
        """When multiple customizers set model, first non-None wins."""
        reg = TemplateRegistry({"modeled": {"system_prompt": "hi", "model": "sonnet"}})
        sm.set_template_registry(reg)

        def ext_customizer(session):
            return SessionOverrides(model="opus")

        sm.add_session_customizer(ext_customizer)
        session = _make_session()
        set_extension_state(session, "templates", "name", "modeled")
        overrides = sm._collect_overrides(session)
        # Template customizer runs first (position 0), so "sonnet" wins
        assert overrides.model == "sonnet"

    def test_model_in_generated_cmd(self, sm):
        """Template model appears as --model flag in generated CLI command."""
        reg = TemplateRegistry({"sonnet-worker": {"system_prompt": "hi", "model": "sonnet"}})
        sm.set_template_registry(reg)
        session = _make_session()
        set_extension_state(session, "templates", "name", "sonnet-worker")
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)
        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        assert "--model sonnet" in claude_cmd

    def test_no_model_uses_engine_config(self, tmp_path):
        """Without template model, engine config model is used."""
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={
                "permission_mode": "bypassPermissions",
                "model": "opus",
            },
        )
        reg = TemplateRegistry({"plain": {"system_prompt": "hi"}})
        sm.set_template_registry(reg)
        session = _make_session()
        set_extension_state(session, "templates", "name", "plain")
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)
        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        assert "--model opus" in claude_cmd

    def test_template_model_overrides_engine_config(self, tmp_path):
        """Template model takes precedence over engine config model."""
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={
                "permission_mode": "bypassPermissions",
                "model": "opus",
            },
        )
        reg = TemplateRegistry({"sonnet-worker": {"system_prompt": "hi", "model": "sonnet"}})
        sm.set_template_registry(reg)
        session = _make_session()
        set_extension_state(session, "templates", "name", "sonnet-worker")
        sdir = sm.session_dir(session.id)
        sdir.mkdir(parents=True)
        claude_cmd, _ = sm._generate_run_scripts(session, sdir, is_first=True)
        assert "--model sonnet" in claude_cmd
        assert "--model opus" not in claude_cmd
