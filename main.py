#!/usr/bin/env python3
"""claude-ext entry point."""

import asyncio
import logging
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


async def main():
    setup_logging()
    log = logging.getLogger("main")

    config = load_config()

    eng_cfg = config.get("engine", {})
    engine = ClaudeEngine(
        model=eng_cfg.get("model"),
        max_turns=eng_cfg.get("max_turns", 0),
        permission_mode=eng_cfg.get("permission_mode", "bypassPermissions"),
        allowed_tools=eng_cfg.get("allowed_tools"),
    )

    # Initialize tmux-backed session manager
    state_dir = Path(config.get("state_dir", "~/.claude-ext")).expanduser()
    session_cfg = config.get("sessions", {})
    engine.init_sessions(
        state_dir,
        max_sessions_per_user=session_cfg.get("max_sessions_per_user", 5),
    )
    await engine.session_manager.recover()
    if engine.bridge:
        await engine.bridge.start()

    registry = Registry(engine, config)

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

    log.info("claude-ext running. Press Ctrl+C to stop.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    log.info("Shutting down...")
    await registry.stop_all()
    if engine.bridge:
        await engine.bridge.stop()
    await engine.session_manager.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
