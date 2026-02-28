"""Extension registry - discovers and manages extension lifecycle."""

import asyncio
import importlib
import logging
from pathlib import Path

from core.engine import ClaudeEngine
from core.extension import Extension

log = logging.getLogger(__name__)

EXTENSIONS_DIR = Path(__file__).parent.parent / "extensions"


class Registry:
    def __init__(self, engine: ClaudeEngine, config: dict):
        self.engine = engine
        self.config = config
        self._extensions: list[Extension] = []

    @property
    def extensions(self) -> list[Extension]:
        """Public read-only view of loaded extensions (in registration order)."""
        return list(self._extensions)

    def discover(self) -> list[str]:
        """Find all extension directories that contain extension.py."""
        found = []
        for child in sorted(EXTENSIONS_DIR.iterdir()):
            if child.is_dir() and (child / "extension.py").exists():
                found.append(child.name)
        return found

    def load(self, names: list[str]) -> None:
        """Import and instantiate the specified extensions."""
        events = self.engine.events
        for name in names:
            try:
                mod = importlib.import_module(f"extensions.{name}.extension")
                cls = mod.ExtensionImpl
                ext: Extension = cls()
                ext_config = self.config.get("extensions", {}).get(name, {})
                ext.configure(self.engine, ext_config)
                self._extensions.append(ext)
                log.info("Loaded extension: %s", name)
            except Exception:
                log.exception("Failed to load extension: %s", name)
                if events:
                    events.log("ext.load_failed", detail={"name": name})

    async def start_all(self) -> None:
        events = self.engine.events
        for ext in self._extensions:
            log.info("Starting extension: %s", ext.name)
            await ext.start()
            if events:
                events.log("ext.started", detail={"name": ext.name})

    async def stop_all(self) -> None:
        events = self.engine.events
        for ext in reversed(self._extensions):
            log.info("Stopping extension: %s", ext.name)
            try:
                await ext.stop()
                if events:
                    events.log("ext.stopped", detail={"name": ext.name})
            except Exception:
                log.exception("Error stopping extension: %s", ext.name)

    async def health_check_all(self) -> dict[str, dict]:
        """Collect health status from all loaded extensions (concurrent)."""

        async def _check(ext: Extension) -> tuple[str, dict]:
            try:
                result = await asyncio.wait_for(ext.health_check(), timeout=5.0)
                return ext.name, result
            except TimeoutError:
                return ext.name, {"status": "error", "detail": "timeout"}
            except Exception as e:
                return ext.name, {"status": "error", "detail": str(e)}

        pairs = await asyncio.gather(*[_check(ext) for ext in self._extensions])
        return dict(pairs)
