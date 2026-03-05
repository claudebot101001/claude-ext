"""Context management extension — token tracking and compaction control.

Provides per-session context window monitoring, manual/auto compaction,
and exposes metrics to other extensions via engine.services["context"].
"""

import asyncio
import logging
import sys
from pathlib import Path

from core.extension import Extension
from core.session import SessionStatus

from .tracker import ContextTracker

log = logging.getLogger(__name__)


class ExtensionImpl(Extension):
    name = "context"

    def configure(self, engine, config):
        super().configure(engine, config)
        self.tracker = ContextTracker()

        auto_cfg = config.get("auto_compact", {})
        self._auto_compact_enabled = auto_cfg.get("enabled", False)
        self._auto_compact_threshold = auto_cfg.get("threshold_pct", 85)
        self._auto_compact_cooldown = auto_cfg.get("cooldown_prompts", 3)

        # Per-session overrides: session_id -> {auto_compact, threshold_pct}
        self._session_config: dict[str, dict] = {}

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # Register MCP server
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        self.sm.register_mcp_server(
            "context",
            {
                "command": sys.executable,
                "args": [mcp_script],
            },
            tools=[
                {"name": "context_status", "description": "Get context window usage"},
                {"name": "context_compact", "description": "Trigger compaction"},
                {"name": "context_configure", "description": "Configure auto-compact"},
            ],
        )

        # Register bridge handlers
        self.engine.bridge.add_handler(self._bridge_handler)

        # Register delivery callback
        self.sm.add_delivery_callback(self._on_delivery)

        # Register as service for cross-extension access
        self.engine.services["context"] = self

        log.info(
            "Context extension started (auto_compact=%s, threshold=%d%%)",
            self._auto_compact_enabled,
            self._auto_compact_threshold,
        )

    async def stop(self) -> None:
        self.engine.services.pop("context", None)
        log.info("Context extension stopped.")

    async def health_check(self) -> dict:
        return {
            "status": "ok",
            "tracked_sessions": len(self.tracker.session_ids()),
            "auto_compact": self._auto_compact_enabled,
        }

    # -- bridge handler -----------------------------------------------------

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        if method == "context_status":
            session_id = params.get("session_id", "")
            return self.tracker.to_dict(session_id)

        if method == "context_compact":
            session_id = params.get("session_id", "")
            return await self._trigger_compact(session_id)

        if method == "context_configure":
            session_id = params.get("session_id", "")
            cfg = self._session_config.setdefault(session_id, {})
            if "auto_compact" in params:
                cfg["auto_compact"] = params["auto_compact"]
            if "threshold_pct" in params:
                cfg["threshold_pct"] = params["threshold_pct"]
            return {"ok": True, "config": cfg}

        return None  # Not our method

    # -- delivery callback --------------------------------------------------

    async def _on_delivery(self, session_id: str, result_text: str, metadata: dict) -> None:
        # Update context fill from streaming assistant events
        if metadata.get("is_stream") and metadata.get("usage"):
            self.tracker.update_from_stream_usage(session_id, metadata["usage"])
            return

        # Update on final result
        if metadata.get("is_final"):
            self.tracker.update_from_result(session_id, metadata)
            self._check_auto_compact(session_id)

            # Clean up tracker for terminated sessions
            session = self.sm.sessions.get(session_id)
            if session and session.status in (
                SessionStatus.STOPPED,
                SessionStatus.DEAD,
            ):
                self._schedule_cleanup(session_id)
            return

    # -- auto-compact -------------------------------------------------------

    def _check_auto_compact(self, session_id: str) -> None:
        """Check if auto-compact should trigger for this session."""
        session = self.sm.sessions.get(session_id)
        if not session:
            return

        # Skip automated sessions (subagent, heartbeat)
        ctx = session.context
        if ctx.get("subagent") or ctx.get("heartbeat_run"):
            return

        # Skip if this was a compact result itself
        if ctx.get("_context_compacting"):
            ctx.pop("_context_compacting", None)
            return

        # Determine config (per-session override or global)
        scfg = self._session_config.get(session_id, {})
        enabled = scfg.get("auto_compact", self._auto_compact_enabled)
        if not enabled:
            return

        threshold = scfg.get("threshold_pct", self._auto_compact_threshold)
        tokens = self.tracker.get(session_id)
        if not tokens:
            return

        fill = tokens.estimated_fill_pct()
        if fill <= threshold:
            return

        # Cooldown check: need N user prompts since last compact
        if tokens.last_compact_at_prompt >= 0:
            prompts_since = tokens.prompt_count - tokens.last_compact_at_prompt
            if prompts_since <= self._auto_compact_cooldown:
                return

        log.info(
            "Auto-compact triggered for session %s (fill=%.1f%%, threshold=%d%%)",
            session_id[:8],
            fill,
            threshold,
        )
        asyncio.create_task(self._auto_compact(session_id))

    async def _auto_compact(self, session_id: str) -> None:
        """Queue a /compact command for the session."""
        result = await self._trigger_compact(session_id, auto=True)
        if result.get("error"):
            log.warning("Auto-compact failed for %s: %s", session_id[:8], result["error"])

    async def _trigger_compact(self, session_id: str, auto: bool = False) -> dict:
        """Send /compact to a session. Returns status dict."""
        session = self.sm.sessions.get(session_id)
        if not session:
            return {"error": "Session not found"}
        if session.status == SessionStatus.DEAD:
            return {"error": "Session is dead"}

        # Mark session so delivery callback suppresses output and skips re-trigger
        session.context["_suppress_delivery"] = True
        session.context["_context_compacting"] = True

        # Update tracker
        tokens = self.tracker.get_or_create(session_id)
        tokens.compaction_count += 1
        tokens.last_compact_at_prompt = tokens.prompt_count

        try:
            position = await self.sm.send_prompt(session_id, "/compact")
        except RuntimeError as e:
            session.context.pop("_suppress_delivery", None)
            session.context.pop("_context_compacting", None)
            return {"error": str(e)}

        return {
            "queued": True,
            "position": position,
            "auto": auto,
            "compaction_count": tokens.compaction_count,
        }

    # -- cleanup ------------------------------------------------------------

    def _schedule_cleanup(self, session_id: str, delay: float = 10.0) -> None:
        asyncio.create_task(self._delayed_cleanup(session_id, delay))

    async def _delayed_cleanup(self, session_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.tracker.remove(session_id)
        self._session_config.pop(session_id, None)
