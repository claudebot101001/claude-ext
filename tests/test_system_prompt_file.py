"""Tests for --system-prompt-file support (custom system prompt replacement)."""

import pytest

from core.session import Session, SessionManager, SessionOverrides


@pytest.fixture
def sm_default(tmp_path):
    """SessionManager with default config (no system_prompt_file)."""
    return SessionManager(
        base_dir=tmp_path,
        engine_config={"permission_mode": "bypassPermissions"},
    )


@pytest.fixture
def sm_replace(tmp_path):
    """SessionManager with system_prompt_file set."""
    base_prompt = tmp_path / "base_prompt.md"
    base_prompt.write_text("# Custom base prompt\nYou are helpful.")
    return SessionManager(
        base_dir=tmp_path,
        engine_config={
            "permission_mode": "bypassPermissions",
            "system_prompt_file": str(base_prompt),
        },
    )


def _make_session(**kwargs):
    defaults = dict(id="test-sid", name="test", slot=1, user_id="u1", working_dir="/tmp")
    defaults.update(kwargs)
    return Session(**defaults)


def _generate_scripts(sm, session=None, is_first=True):
    """Helper: generate run scripts and return claude_cmd content."""
    if session is None:
        session = _make_session()
    sdir = sm.session_dir(session.id)
    sdir.mkdir(parents=True, exist_ok=True)
    claude_cmd, _run_sh = sm._generate_run_scripts(session, sdir, is_first=is_first)
    return claude_cmd


# ---------------------------------------------------------------------------
# Mode A: default behavior (no system_prompt_file)
# ---------------------------------------------------------------------------


class TestDefaultMode:
    def test_append_system_prompt_used(self, sm_default):
        sm_default.add_system_prompt("Test extension prompt")
        cmd = _generate_scripts(sm_default)
        assert '--append-system-prompt "$SYS_PROMPT"' in cmd
        assert "SYS_PROMPT=$(cat" in cmd

    def test_no_system_prompt_file_flag(self, sm_default):
        sm_default.add_system_prompt("Test prompt")
        cmd = _generate_scripts(sm_default)
        assert "--system-prompt-file" not in cmd

    def test_no_prompts_no_flags(self, sm_default):
        cmd = _generate_scripts(sm_default)
        assert "--append-system-prompt" not in cmd
        assert "--system-prompt-file" not in cmd
        assert "SYS_PROMPT" not in cmd


# ---------------------------------------------------------------------------
# Mode B: replace mode (system_prompt_file set)
# ---------------------------------------------------------------------------


class TestReplaceMode:
    def test_system_prompt_file_flag_present(self, sm_replace):
        cmd = _generate_scripts(sm_replace)
        assert "--system-prompt-file" in cmd

    def test_no_shell_variable_expansion(self, sm_replace):
        sm_replace.add_system_prompt("Extension prompt here")
        cmd = _generate_scripts(sm_replace)
        assert "SYS_PROMPT=$(cat" not in cmd
        assert '--append-system-prompt "$SYS_PROMPT"' not in cmd

    def test_append_file_flag_with_extensions(self, sm_replace):
        sm_replace.add_system_prompt("Extension prompt content")
        cmd = _generate_scripts(sm_replace)
        assert "--append-system-prompt-file" in cmd

    def test_no_append_without_extensions(self, sm_replace):
        cmd = _generate_scripts(sm_replace)
        assert "--append-system-prompt-file" not in cmd
        assert "--system-prompt-file" in cmd

    def test_extension_prompt_file_written(self, sm_replace):
        sm_replace.add_system_prompt("First fragment")
        sm_replace.add_system_prompt("Second fragment")
        session = _make_session()
        sdir = sm_replace.session_dir(session.id)
        sdir.mkdir(parents=True, exist_ok=True)
        sm_replace._generate_run_scripts(session, sdir, is_first=True)

        prompt_file = sdir / "system_prompt.txt"
        assert prompt_file.exists()
        content = prompt_file.read_text()
        assert "First fragment" in content
        assert "Second fragment" in content

    def test_base_prompt_path_in_command(self, sm_replace, tmp_path):
        cmd = _generate_scripts(sm_replace)
        base_path = str(tmp_path / "base_prompt.md")
        assert base_path in cmd


# ---------------------------------------------------------------------------
# Customizer integration
# ---------------------------------------------------------------------------


class TestCustomizerInReplaceMode:
    def test_customizer_prompts_included(self, sm_replace):
        def customizer(session):
            return SessionOverrides(extra_system_prompt=["Customizer-added prompt"])

        sm_replace.add_session_customizer(customizer)
        session = _make_session()
        sdir = sm_replace.session_dir(session.id)
        sdir.mkdir(parents=True, exist_ok=True)
        sm_replace._generate_run_scripts(session, sdir, is_first=True)

        prompt_file = sdir / "system_prompt.txt"
        content = prompt_file.read_text()
        assert "Customizer-added prompt" in content

    def test_tagged_prompts_excluded(self, sm_replace):
        sm_replace.add_system_prompt("Keep this", mcp_server=None)
        sm_replace.add_system_prompt("Remove this", mcp_server="excluded_server")

        def customizer(session):
            return SessionOverrides(exclude_mcp_servers={"excluded_server"})

        sm_replace.add_session_customizer(customizer)
        session = _make_session()
        sdir = sm_replace.session_dir(session.id)
        sdir.mkdir(parents=True, exist_ok=True)
        sm_replace._generate_run_scripts(session, sdir, is_first=True)

        prompt_file = sdir / "system_prompt.txt"
        content = prompt_file.read_text()
        assert "Keep this" in content
        assert "Remove this" not in content


# ---------------------------------------------------------------------------
# Compact prompt resolution
# ---------------------------------------------------------------------------


class TestCompactResolution:
    def test_compact_resolves_to_bundled_file(self):
        from pathlib import Path

        compact_path = Path(__file__).parent.parent / "core" / "compact_prompt.md"
        assert compact_path.exists()
        content = compact_path.read_text()
        assert "Tool Routing" in content
        assert "Git Safety" in content
