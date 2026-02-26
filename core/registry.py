"""Extension registry - discovers and manages extension lifecycle."""

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

    def discover(self) -> list[str]:
        """Find all extension directories that contain extension.py."""
        found = []
        for child in sorted(EXTENSIONS_DIR.iterdir()):
            if child.is_dir() and (child / "extension.py").exists():
                found.append(child.name)
        return found

    def load(self, names: list[str]) -> None:
        """Import and instantiate the specified extensions."""
        for name in names:
            try:
                mod = importlib.import_module(f"extensions.{name}.extension")
                cls = getattr(mod, "ExtensionImpl")
                ext: Extension = cls()
                ext_config = self.config.get("extensions", {}).get(name, {})
                ext.configure(self.engine, ext_config)
                self._extensions.append(ext)
                log.info("Loaded extension: %s", name)
            except Exception:
                log.exception("Failed to load extension: %s", name)

    async def start_all(self) -> None:
        for ext in self._extensions:
            log.info("Starting extension: %s", ext.name)
            await ext.start()

    async def stop_all(self) -> None:
        for ext in reversed(self._extensions):
            log.info("Stopping extension: %s", ext.name)
            try:
                await ext.stop()
            except Exception:
                log.exception("Error stopping extension: %s", ext.name)
