"""Tests for core/templates.py — Template dataclass, TemplateRegistry, and session init helper."""

import pytest

from core.session_context import get_extension_state
from core.templates import Template, TemplateRegistry, resolve_template_session_init


class TestTemplate:
    def test_defaults(self):
        t = Template(name="test")
        assert t.description == ""
        assert t.system_prompt == ""
        assert t.working_dir is None
        assert t.context_defaults == {}
        assert t.disallowed_tools == []
        assert t.allowed_tools is None
        assert t.mcp_servers is None
        assert t.exclude_mcp_servers == set()
        assert t.exclude_mcp_tags == set()
        assert t.auto_cleanup is True
        assert t.inject_identity is True

    def test_full_construction(self):
        t = Template(
            name="researcher",
            description="Research agent",
            system_prompt="You are a researcher.",
            working_dir="~/research",
            context_defaults={"magma": True},
            disallowed_tools=["Write", "Edit"],
            allowed_tools=["Read", "Grep"],
            mcp_servers={"arxiv", "browser"},
            exclude_mcp_servers={"subagent"},
            exclude_mcp_tags={"read_only_worker_exclude"},
            auto_cleanup=False,
        )
        assert t.name == "researcher"
        assert t.allowed_tools == ["Read", "Grep"]
        assert t.mcp_servers == {"arxiv", "browser"}
        assert t.auto_cleanup is False


class TestTemplateRegistry:
    def test_builtins_loaded(self):
        reg = TemplateRegistry()
        assert reg.has("coder")
        assert reg.has("reviewer")
        assert reg.has("researcher")
        assert (
            len(reg.names()) == 3
        )  # coder, reviewer, researcher (audit templates moved to extension)

    def test_builtin_coder(self):
        reg = TemplateRegistry()
        t = reg.get("coder")
        assert t is not None
        assert t.auto_cleanup is True
        assert t.disallowed_tools == []

    def test_builtin_reviewer(self):
        reg = TemplateRegistry()
        t = reg.get("reviewer")
        assert t is not None
        assert t.auto_cleanup is False
        assert "Write" in t.disallowed_tools
        assert "Edit" in t.disallowed_tools

    def test_config_override_builtin(self):
        reg = TemplateRegistry(
            {
                "coder": {
                    "description": "Custom coder",
                    "system_prompt": "Be fast.",
                    "auto_cleanup": False,
                }
            }
        )
        t = reg.require("coder")
        assert t.description == "Custom coder"
        assert t.system_prompt == "Be fast."
        assert t.auto_cleanup is False

    def test_config_add_custom(self):
        reg = TemplateRegistry(
            {
                "auditor": {
                    "description": "Smart contract auditor",
                    "system_prompt": "Audit the contract.",
                    "disallowed_tools": ["Write"],
                    "mcp_servers": ["browser", "memory"],
                    "auto_cleanup": False,
                }
            }
        )
        assert reg.has("auditor")
        t = reg.require("auditor")
        assert t.mcp_servers == {"browser", "memory"}
        assert t.disallowed_tools == ["Write"]

    def test_get_missing_returns_none(self):
        reg = TemplateRegistry()
        assert reg.get("nonexistent") is None

    def test_require_missing_raises(self):
        reg = TemplateRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            reg.require("nonexistent")

    def test_names_sorted(self):
        reg = TemplateRegistry()
        names = reg.names()
        assert names == sorted(names)

    def test_list_returns_all(self):
        reg = TemplateRegistry()
        templates = reg.list()
        assert len(templates) == 3
        assert all(isinstance(t, Template) for t in templates)

    def test_none_config(self):
        reg = TemplateRegistry(None)
        assert reg.has("coder")

    def test_mcp_servers_none_vs_set(self):
        """mcp_servers=None means inherit global; set means allowlist."""
        reg = TemplateRegistry(
            {
                "open": {"description": "No MCP restriction"},
                "restricted": {
                    "description": "Only arxiv",
                    "mcp_servers": ["arxiv"],
                },
            }
        )
        assert reg.require("open").mcp_servers is None
        assert reg.require("restricted").mcp_servers == {"arxiv"}

    def test_allowed_tools_none_vs_list(self):
        """allowed_tools=None means inherit global; list means allowlist."""
        reg = TemplateRegistry(
            {
                "open": {"description": "No tool restriction"},
                "restricted": {
                    "description": "Only Read",
                    "allowed_tools": ["Read"],
                },
            }
        )
        assert reg.require("open").allowed_tools is None
        assert reg.require("restricted").allowed_tools == ["Read"]

    def test_prompt_files_loaded(self):
        """Built-in templates load system_prompt from .md files."""
        reg = TemplateRegistry()
        coder = reg.require("coder")
        assert coder.system_prompt != ""
        assert "coding worker" in coder.system_prompt.lower()

    def test_inject_identity_default_true(self):
        reg = TemplateRegistry()
        assert reg.require("coder").inject_identity is True

    def test_config_inject_identity(self):
        reg = TemplateRegistry({"worker": {"description": "No identity", "inject_identity": False}})
        assert reg.require("worker").inject_identity is False

    def test_config_system_prompt_file(self, tmp_path):
        prompt_file = tmp_path / "custom.md"
        prompt_file.write_text("Custom prompt from file.")
        reg = TemplateRegistry({"custom": {"system_prompt_file": str(prompt_file)}})
        assert reg.require("custom").system_prompt == "Custom prompt from file."

    def test_config_system_prompt_file_missing(self):
        """Missing system_prompt_file falls back to inline (empty)."""
        reg = TemplateRegistry({"custom": {"system_prompt_file": "/nonexistent/path.md"}})
        assert reg.require("custom").system_prompt == ""

    def test_deleted_templates_gone(self):
        reg = TemplateRegistry()
        assert reg.get("codex_reviewer") is None
        assert reg.get("codex_hunter") is None
        assert reg.get("scope-validator") is None


class TestResolveTemplateSessionInit:
    """Tests for resolve_template_session_init() core helper."""

    def test_no_template(self):
        reg = TemplateRegistry()
        wd, ctx = resolve_template_session_init(reg, None, default_working_dir="/default")
        assert wd == "/default"
        assert ctx == {}

    def test_template_sets_identity(self):
        reg = TemplateRegistry()
        wd, ctx = resolve_template_session_init(reg, "coder", default_working_dir="/default")
        assert wd == "/default"
        assert get_extension_state(ctx, "templates", "name") == "coder"

    def test_template_working_dir(self):
        reg = TemplateRegistry({"custom": {"working_dir": "/opt/custom"}})
        wd, _ctx = resolve_template_session_init(reg, "custom", default_working_dir="/default")
        assert wd == "/opt/custom"

    def test_explicit_working_dir_overrides_template(self):
        reg = TemplateRegistry({"custom": {"working_dir": "/opt/custom"}})
        wd, _ctx = resolve_template_session_init(
            reg,
            "custom",
            default_working_dir="/default",
            explicit_working_dir="/explicit",
        )
        assert wd == "/explicit"

    def test_explicit_working_dir_no_template(self):
        reg = TemplateRegistry()
        wd, _ctx = resolve_template_session_init(
            reg, None, default_working_dir="/default", explicit_working_dir="/explicit"
        )
        assert wd == "/explicit"

    def test_context_defaults_applied(self):
        reg = TemplateRegistry({"custom": {"context_defaults": {"magma": True, "priority": "low"}}})
        _wd, ctx = resolve_template_session_init(reg, "custom", default_working_dir="/default")
        assert ctx["magma"] is True
        assert ctx["priority"] == "low"

    def test_base_context_overrides_defaults(self):
        reg = TemplateRegistry(
            {"custom": {"context_defaults": {"chat_id": 999, "priority": "low"}}}
        )
        _wd, ctx = resolve_template_session_init(
            reg,
            "custom",
            default_working_dir="/default",
            base_context={"chat_id": 12345},
        )
        # base_context wins over context_defaults
        assert ctx["chat_id"] == 12345
        # context_defaults still applied for non-conflicting keys
        assert ctx["priority"] == "low"

    def test_unknown_template_raises(self):
        reg = TemplateRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            resolve_template_session_init(reg, "nonexistent", default_working_dir="/default")

    def test_no_template_identity_without_template(self):
        """When template_name is None, no _extensions.templates.name is set."""
        reg = TemplateRegistry()
        _wd, ctx = resolve_template_session_init(
            reg, None, default_working_dir="/default", base_context={"chat_id": 1}
        )
        assert get_extension_state(ctx, "templates", "name") is None

    def test_tilde_expanded(self):
        """~ in template working_dir is expanded."""
        import os

        reg = TemplateRegistry({"custom": {"working_dir": "~/myproject"}})
        wd, _ctx = resolve_template_session_init(reg, "custom", default_working_dir="/default")
        assert wd == os.path.expanduser("~/myproject")
        assert "~" not in wd

    def test_relative_path_resolved(self):
        """Relative template working_dir resolves relative to default_working_dir."""
        reg = TemplateRegistry({"custom": {"working_dir": "subdir/proj"}})
        wd, _ctx = resolve_template_session_init(reg, "custom", default_working_dir="/home/user")
        assert wd == "/home/user/subdir/proj"

    def test_absolute_path_unchanged(self):
        """Absolute template working_dir is returned as-is."""
        reg = TemplateRegistry({"custom": {"working_dir": "/opt/projects"}})
        wd, _ctx = resolve_template_session_init(reg, "custom", default_working_dir="/default")
        assert wd == "/opt/projects"

    def test_explicit_tilde_expanded(self):
        """~ in explicit_working_dir is also expanded."""
        import os

        reg = TemplateRegistry()
        wd, _ctx = resolve_template_session_init(
            reg, None, default_working_dir="/default", explicit_working_dir="~/work"
        )
        assert wd == os.path.expanduser("~/work")
