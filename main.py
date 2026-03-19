#!/usr/bin/env python3
"""claude-ext entry point."""

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

import yaml

from core.engine import ClaudeEngine
from core.registry import Registry
from core.templates import TemplateRegistry


def _load_dotenv(env_path: str = ".env") -> None:
    """Load .env file into os.environ (simple KEY=VALUE, no shell expansion)."""
    p = Path(env_path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _resolve_env_vars(text: str) -> str:
    """Replace ${VAR} references with environment variable values."""
    import re

    def _sub(m):
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            logging.getLogger(__name__).warning("Env var %s not set (referenced in config)", var)
            return m.group(0)  # leave unresolved
        return val

    return re.sub(r"\$\{([^}]+)\}", _sub, text)


def load_config(path: str = "config.yaml") -> dict:
    _load_dotenv()
    with open(path) as f:
        raw = f.read()
    resolved = _resolve_env_vars(raw)
    return yaml.safe_load(resolved)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress httpx INFO logs — they leak Telegram bot tokens in URLs
    logging.getLogger("httpx").setLevel(logging.WARNING)


def add_file_logging(state_dir: Path):
    """Add rotating file handler after config is loaded."""
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    file_handler = logging.handlers.RotatingFileHandler(
        state_dir / "claude-ext.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.getLogger().addHandler(file_handler)


async def _graceful_shutdown(registry, engine):
    """Shutdown sequence: extensions → bridge → sessions."""
    await registry.stop_all()
    if engine.bridge:
        await engine.bridge.stop()
    await engine.session_manager.shutdown()


async def main():
    setup_logging()
    log = logging.getLogger("main")

    config = load_config()

    eng_cfg = config.get("engine", {})
    disallowed_tools = eng_cfg.get("disallowed_tools") or []
    essential_tools = {"Read", "Edit", "Write", "Bash", "Glob", "Grep"}
    blocked_essential = essential_tools & set(disallowed_tools)
    if blocked_essential:
        log.warning(
            "disallowed_tools contains essential tools: %s — sessions may not function correctly",
            ", ".join(sorted(blocked_essential)),
        )

    engine = ClaudeEngine(
        model=eng_cfg.get("model"),
        max_turns=eng_cfg.get("max_turns", 0),
        permission_mode=eng_cfg.get("permission_mode", "bypassPermissions"),
        allowed_tools=eng_cfg.get("allowed_tools"),
        disallowed_tools=disallowed_tools or None,
        gateway_mode=bool(eng_cfg.get("gateway_mode")),
    )

    # Initialize tmux-backed session manager
    state_dir = Path(config.get("state_dir", "~/.claude-ext")).expanduser()
    add_file_logging(state_dir)
    session_cfg = config.get("sessions", {})
    engine.init_sessions(
        state_dir,
        max_sessions_per_user=session_cfg.get("max_sessions_per_user", 5),
        session_timeout=session_cfg.get("session_timeout", 7200),
    )
    await engine.session_manager.recover()

    # Initialize template registry (before extensions, so customizer runs first)
    engine.templates = TemplateRegistry(config.get("templates"))
    engine.session_manager.set_template_registry(engine.templates)

    # Write PID file for external restart (e.g. kill $(cat pidfile))
    pid_file = state_dir / "claude-ext.pid"
    pid_file.write_text(str(os.getpid()))
    log.info("PID %d written to %s", os.getpid(), pid_file)
    if engine.bridge:
        await engine.bridge.start()

    registry = Registry(engine, config)
    engine.registry = registry

    enabled = config.get("enabled", [])
    available = registry.discover()
    to_load = [e for e in enabled if e in available]

    if not to_load:
        log.error("No extensions to load. Check config.yaml 'enabled' list.")
        sys.exit(1)

    missing = set(enabled) - set(available)
    if missing:
        log.warning("Extensions not found: %s", missing)

    registry.load(to_load)
    normalized = engine.session_manager.normalize_session_contexts()
    if normalized:
        log.info("Normalized legacy context state for %d recovered session(s)", normalized)
    await registry.start_all()

    # Gateway mode system prompt
    if engine.gateway_mode:
        engine.session_manager.add_system_prompt(
            "MCP tools use a gateway pattern: each extension is one tool. "
            "Call with action='help' to discover commands, then "
            "action='<command>' with params={...}."
        )
        log.info("Gateway mode enabled — MCP tools consolidated")

    log.info("claude-ext running. Press Ctrl+C to stop.")

    # -- Signal handlers -------------------------------------------------------

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    def _handle_sighup():
        """Reload config.yaml on SIGHUP. Sync callback (signal handler requirement)."""
        log.info("SIGHUP received, reloading config...")
        try:
            new_config = load_config()
        except Exception:
            log.exception("Config reload failed, keeping current config")
            return

        # Update session manager
        sess_cfg = new_config.get("sessions", {})
        new_max = sess_cfg.get("max_sessions_per_user")
        if new_max is not None:
            engine.session_manager.max_sessions_per_user = new_max
        new_timeout = sess_cfg.get("session_timeout")
        if new_timeout is not None:
            engine.session_manager.session_timeout = float(new_timeout)

        # Update engine (max_turns only — model/permission_mode not safe to hot-reload)
        new_eng_cfg = new_config.get("engine", {})
        engine.max_turns = new_eng_cfg.get("max_turns", engine.max_turns)

        # Reload templates
        new_templates = TemplateRegistry(new_config.get("templates"))
        engine.templates = new_templates
        engine.session_manager.set_template_registry(new_templates)

        # Notify extensions
        for ext in registry.extensions:
            ext_config = new_config.get("extensions", {}).get(ext.name, {})
            try:
                ext.reconfigure(ext_config)
            except Exception:
                log.exception("Extension %s reconfigure failed", ext.name)

        registry.config = new_config
        if engine.events:
            engine.events.log("config.reloaded")
        log.info("Config reload complete")

    loop.add_signal_handler(signal.SIGHUP, _handle_sighup)

    # -- Wait for stop, then shutdown with timeout -----------------------------

    try:
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        try:
            await asyncio.wait_for(_graceful_shutdown(registry, engine), timeout=15.0)
        except TimeoutError:
            log.warning("Graceful shutdown timed out after 15s, forcing exit")
            pid_file.unlink(missing_ok=True)
            os._exit(1)
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
