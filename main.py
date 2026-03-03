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


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


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

    # Resolve system_prompt_file: "compact" -> bundled path, str -> absolute, null -> None
    raw_spf = eng_cfg.get("system_prompt_file")
    resolved_spf = None
    if raw_spf == "compact":
        resolved_spf = str(Path(__file__).parent / "core" / "compact_prompt.md")
    elif raw_spf:
        resolved_spf = str(Path(raw_spf).expanduser().resolve())
        if not Path(resolved_spf).is_file():
            log.error("system_prompt_file not found: %s", resolved_spf)
            sys.exit(1)

    engine = ClaudeEngine(
        model=eng_cfg.get("model"),
        max_turns=eng_cfg.get("max_turns", 0),
        permission_mode=eng_cfg.get("permission_mode", "bypassPermissions"),
        allowed_tools=eng_cfg.get("allowed_tools"),
        disallowed_tools=disallowed_tools or None,
        gateway_mode=bool(eng_cfg.get("gateway_mode")),
        system_prompt_file=resolved_spf,
    )

    # Initialize tmux-backed session manager
    state_dir = Path(config.get("state_dir", "~/.claude-ext")).expanduser()
    add_file_logging(state_dir)
    session_cfg = config.get("sessions", {})
    engine.init_sessions(
        state_dir,
        max_sessions_per_user=session_cfg.get("max_sessions_per_user", 5),
    )
    await engine.session_manager.recover()

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

        # Update engine (max_turns only — model/permission_mode not safe to hot-reload)
        new_eng_cfg = new_config.get("engine", {})
        engine.max_turns = new_eng_cfg.get("max_turns", engine.max_turns)

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
