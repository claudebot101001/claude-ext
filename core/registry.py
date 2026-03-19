"""Extension registry - discovers and manages extension lifecycle."""

import asyncio
import copy
import importlib
import logging
from dataclasses import dataclass
from pathlib import Path

from core.engine import ClaudeEngine
from core.extension import Extension

log = logging.getLogger(__name__)

EXTENSIONS_DIR = Path(__file__).parent.parent / "extensions"


@dataclass
class _RuntimeSnapshot:
    services: dict
    bridge_handlers: list | None
    delivery_callbacks: list
    pending_deliveries: list
    delivery_flush_enabled: bool
    mcp_servers: dict
    mcp_tool_meta: dict
    mcp_server_tags: dict
    system_prompt_parts: list
    env_unset: list
    disallowed_tools: list
    session_customizers: list
    pre_prompt_hooks: list
    session_taggers: list


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

    def _snapshot_runtime_state(self) -> _RuntimeSnapshot:
        sm = self.engine.session_manager
        bridge = self.engine.bridge
        return _RuntimeSnapshot(
            services=dict(self.engine.services),
            bridge_handlers=list(bridge._handlers) if bridge else None,
            delivery_callbacks=list(sm._delivery_cbs),
            pending_deliveries=list(sm._pending_deliveries),
            delivery_flush_enabled=sm._delivery_flush_enabled,
            mcp_servers=copy.deepcopy(sm._mcp_servers),
            mcp_tool_meta=copy.deepcopy(sm._mcp_tool_meta),
            mcp_server_tags=copy.deepcopy(sm._mcp_server_tags),
            system_prompt_parts=list(sm._system_prompt_parts),
            env_unset=list(sm._env_unset),
            disallowed_tools=list(sm._disallowed_tools),
            session_customizers=list(sm._session_customizers),
            pre_prompt_hooks=list(sm._pre_prompt_hooks),
            session_taggers=list(sm._session_taggers),
        )

    def _restore_runtime_state(self, snapshot: _RuntimeSnapshot) -> None:
        sm = self.engine.session_manager
        bridge = self.engine.bridge

        self.engine.services.clear()
        self.engine.services.update(snapshot.services)

        if bridge and snapshot.bridge_handlers is not None:
            bridge._handlers = list(snapshot.bridge_handlers)

        sm._delivery_cbs = list(snapshot.delivery_callbacks)
        sm._pending_deliveries = list(snapshot.pending_deliveries)
        sm._delivery_flush_enabled = snapshot.delivery_flush_enabled
        sm._mcp_servers = copy.deepcopy(snapshot.mcp_servers)
        sm._mcp_tool_meta = copy.deepcopy(snapshot.mcp_tool_meta)
        sm._mcp_server_tags = copy.deepcopy(snapshot.mcp_server_tags)
        sm._system_prompt_parts = list(snapshot.system_prompt_parts)
        sm._env_unset = list(snapshot.env_unset)
        sm._disallowed_tools = list(snapshot.disallowed_tools)
        sm._session_customizers = list(snapshot.session_customizers)
        sm._pre_prompt_hooks = list(snapshot.pre_prompt_hooks)
        sm._session_taggers = list(snapshot.session_taggers)

    async def _rollback_started(
        self,
        started: list[Extension],
        *,
        failed_ext: Extension | None = None,
    ) -> None:
        events = self.engine.events
        to_stop = []
        if failed_ext is not None:
            to_stop.append(failed_ext)
        to_stop.extend(reversed(started))

        for ext in to_stop:
            log.info("Rollback stopping extension: %s", ext.name)
            try:
                await ext.stop()
                if events:
                    events.log("ext.rolled_back", detail={"name": ext.name})
            except Exception:
                log.exception("Error rolling back extension: %s", ext.name)
                if events:
                    events.log("ext.rollback_failed", detail={"name": ext.name})

    def _validate_and_sort(self) -> None:
        """Validate hard dependencies exist and topologically sort extensions."""
        loaded_names = {ext.name for ext in self._extensions}

        # Validate hard dependencies
        for ext in self._extensions:
            for dep in ext.dependencies:
                if dep not in loaded_names:
                    raise RuntimeError(
                        f"Extension {ext.name!r} requires {dep!r} but it is not enabled. "
                        f"Add {dep!r} to the 'enabled' list in config.yaml."
                    )

        # Build dependency graph (hard + soft that are loaded)
        order: list[Extension] = []
        visited: set[str] = set()
        visiting: set[str] = set()  # cycle detection
        ext_map = {ext.name: ext for ext in self._extensions}

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                log.warning("Dependency cycle detected involving %r, breaking", name)
                return
            visiting.add(name)
            ext = ext_map.get(name)
            if ext:
                # Process hard deps first, then soft deps
                for dep in ext.dependencies:
                    visit(dep)
                for dep in ext.soft_dependencies:
                    if dep in ext_map:
                        visit(dep)
            visiting.discard(name)
            visited.add(name)
            if ext:
                order.append(ext)

        for ext in self._extensions:
            visit(ext.name)

        if [e.name for e in order] != [e.name for e in self._extensions]:
            log.info(
                "Extensions reordered by dependencies: %s",
                ", ".join(e.name for e in order),
            )
        self._extensions = order

    async def start_all(self) -> None:
        self._validate_and_sort()
        events = self.engine.events
        sm = self.engine.session_manager
        initial_snapshot = self._snapshot_runtime_state()
        started: list[Extension] = []

        sm.set_delivery_flush_enabled(False)
        try:
            for ext in self._extensions:
                ext_snapshot = self._snapshot_runtime_state()
                log.info("Starting extension: %s", ext.name)
                try:
                    await ext.start()
                except Exception:
                    log.exception("Extension %s failed to start", ext.name)
                    self._restore_runtime_state(ext_snapshot)
                    if events:
                        events.log("ext.start_failed", detail={"name": ext.name})
                    await self._rollback_started(started, failed_ext=ext)
                    self._restore_runtime_state(initial_snapshot)
                    raise

                started.append(ext)
                if events:
                    events.log("ext.started", detail={"name": ext.name})
        except Exception:
            sm.set_delivery_flush_enabled(True)
            raise

        sm.set_delivery_flush_enabled(True)
        sm.flush_pending_deliveries()

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
