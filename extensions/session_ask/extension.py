"""Cross-session RPC extension — lets sessions ask questions to other sessions.

Registers MCP tools (session_ask, session_reply, session_list) and bridge
handlers.  When session A calls session_ask, the question is injected as a
prompt into session B.  Session B processes it and calls session_reply, which
resolves the PendingStore entry and unblocks session A.

Data flow::

    Session A → MCP session_ask → bridge.call("session_ask") → BridgeServer handler
      → PendingStore.register + SessionManager.send_prompt(B, question)
      → B receives question as prompt → B calls session_reply MCP tool
      → bridge.call("session_reply") → PendingStore.resolve → A unblocks
"""

import asyncio
import logging
import sys
from pathlib import Path

from core.extension import Extension
from core.session import SessionStatus

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You have cross-session communication tools available. \
Use session_ask to send a question to another session and wait for a reply. \
If you receive an inter-session question, you MUST respond using session_reply \
with the request_id from the question. \
Use session_list to discover available sessions."""


class ExtensionImpl(Extension):
    name = "session_ask"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._timeout = float(config.get("timeout", 300))
        self._max_question_length = int(config.get("max_question_length", 2000))

        # Maps pending_key → asking_session_id for cleanup tracking.
        # We track manually rather than using cancel_for_session() because
        # that would indiscriminately cancel pending entries from other
        # extensions (ask_user, subagent) sharing the same PendingStore.
        self._active_asks: dict[str, str] = {}

    @property
    def sm(self):
        return self.engine.session_manager

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        # 1. Register MCP server
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server(
            "session_ask",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {
                    "SESSION_ASK_TIMEOUT": str(self._timeout),
                },
            },
            tools=[
                {
                    "name": "session_ask",
                    "description": "Send a question to another session and wait for reply",
                },
                {
                    "name": "session_reply",
                    "description": "Reply to an inter-session question",
                },
                {
                    "name": "session_list",
                    "description": "List active sessions for the current user",
                },
            ],
        )

        # 2. Bridge handler
        if self.engine.bridge:
            self.engine.bridge.add_handler(self._bridge_handler)

        # 3. System prompt
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="session_ask")

        # 4. Delivery callback — cancel pending asks when asking session stops
        self.sm.add_delivery_callback(self._on_delivery)

        # 5. Service registry
        self.engine.services["session_ask"] = self

        log.info("session_ask extension started (timeout=%ss)", self._timeout)

    async def stop(self) -> None:
        # Cancel all active pending entries (mirrors subagent stop() pattern)
        for pending_key, _asking_sid in list(self._active_asks.items()):
            self.engine.pending.resolve(pending_key, {"error": "Extension shutting down"})
        self._active_asks.clear()

        self.engine.services.pop("session_ask", None)
        log.info("session_ask extension stopped.")

    async def health_check(self) -> dict:
        return {"status": "ok", "active_asks": len(self._active_asks)}

    # -- delivery callback ----------------------------------------------------

    async def _on_delivery(self, session_id: str, result_text: str, metadata: dict) -> None:
        """Cancel pending asks when the asking session is stopped or errors."""
        if not metadata.get("is_final"):
            return

        is_stopped = metadata.get("is_stopped", False)
        is_error = metadata.get("is_error", False)
        if not (is_stopped or is_error):
            return

        # Check if this session is the asker in any active asks
        keys_to_cancel = [
            key for key, asking_sid in self._active_asks.items() if asking_sid == session_id
        ]
        for key in keys_to_cancel:
            self._active_asks.pop(key, None)
            self.engine.pending.resolve(key, {"error": "Asking session stopped", "cancelled": True})
            log.info(
                "Cancelled session_ask pending %s (asking session %s stopped)",
                key[:8],
                session_id[:8],
            )

        # Also check if this session is a target — proactively unblock the asker
        for key in list(self._active_asks):
            entry = self.engine.pending.get(key)
            if entry and entry.data.get("target_session_id") == session_id:
                self._active_asks.pop(key, None)
                self.engine.pending.resolve(
                    key, {"error": f"Target session {session_id[:8]} stopped/failed"}
                )
                log.info(
                    "Cancelled session_ask pending %s (target session %s stopped)",
                    key[:8],
                    session_id[:8],
                )

    # -- bridge handler -------------------------------------------------------

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        handlers = {
            "session_ask": self._handle_ask,
            "session_reply": self._handle_reply,
            "session_list": self._handle_list,
        }
        handler = handlers.get(method)
        if handler is None:
            return None  # not ours
        try:
            return await handler(params)
        except Exception as e:
            log.exception("Error in session_ask handler %s", method)
            return {"error": str(e)}

    # -- session_ask ----------------------------------------------------------

    async def _handle_ask(self, params: dict) -> dict:
        """Register pending, inject question into target, block until reply."""
        asking_session_id = params.get("session_id", "")
        target_session_id = params.get("target_session_id", "")
        question = params.get("question", "")

        if not question:
            return {"error": "question is required"}
        if not target_session_id:
            return {"error": "target_session_id is required"}
        if asking_session_id == target_session_id:
            return {"error": "Cannot ask yourself"}

        # Truncate overly long questions
        if len(question) > self._max_question_length:
            question = question[: self._max_question_length] + "... [truncated]"

        # Validate asking session
        asking_session = self.sm.sessions.get(asking_session_id)
        if not asking_session:
            return {"error": f"Asking session {asking_session_id[:8]} not found"}

        # Validate target session
        target_session = self.sm.sessions.get(target_session_id)
        if not target_session:
            return {"error": f"Target session {target_session_id[:8]} not found"}

        # Same-user security boundary
        if asking_session.user_id != target_session.user_id:
            return {"error": "Cannot ask sessions owned by a different user"}

        # Check target is not DEAD
        if target_session.status == SessionStatus.DEAD:
            return {"error": f"Target session {target_session_id[:8]} is dead"}

        # 1. Register pending entry (keyed to asking session)
        pending = self.engine.pending
        entry = pending.register(
            session_id=asking_session_id,
            data={
                "type": "session_ask",
                "target_session_id": target_session_id,
                "question": question,
            },
            timeout=self._timeout,
        )

        # 2. Track for cleanup
        self._active_asks[entry.key] = asking_session_id

        # 3. Build and inject the question prompt into target session
        asking_name = asking_session.name or asking_session_id[:8]
        prompt = (
            f"## Inter-Session Request [request_id: {entry.key}]\n\n"
            f'Session "{asking_name}" (ID: {asking_session_id[:8]}) '
            f"is asking you:\n\n"
            f"{question}\n\n"
            f"---\n"
            f"IMPORTANT: You MUST respond using the `session_reply` MCP tool "
            f"with the request_id above.\n"
            f'Call: session_reply(request_id="{entry.key}", reply="your answer")\n'
            f"If you cannot answer, still call session_reply with an explanation."
        )

        try:
            await self.sm.send_prompt(target_session_id, prompt)
        except (KeyError, RuntimeError) as e:
            # TOCTOU: target destroyed/dead between validation and send_prompt.
            # Clean up both _active_asks and the PendingStore entry directly
            # (we never call wait(), so its finally-block won't run to clean up).
            self._active_asks.pop(entry.key, None)
            pending.resolve(entry.key, None)
            pending._entries.pop(entry.key, None)
            return {"error": f"Failed to deliver question: {e}"}

        # Log event
        if self.engine.events:
            self.engine.events.log(
                "session_ask.sent",
                session_id=asking_session_id,
                detail={
                    "target": target_session_id[:8],
                    "request_id": entry.key,
                    "question_length": len(question),
                },
            )

        # 4. Block until reply, timeout, or cancellation
        try:
            result = await pending.wait(entry.key)
            if result is None:
                return {"reply": "", "error": "Question could not be delivered"}
            if isinstance(result, dict):
                if result.get("cancelled"):
                    return {"reply": "", "cancelled": True}
                if result.get("error"):
                    return {"reply": "", "error": result["error"]}
                return {"reply": result.get("reply", str(result)), "request_id": entry.key}
            return {"reply": str(result), "request_id": entry.key}
        except TimeoutError:
            if self.engine.events:
                self.engine.events.log(
                    "session_ask.timeout",
                    session_id=asking_session_id,
                    detail={"target": target_session_id[:8], "request_id": entry.key},
                )
            return {"reply": "", "timed_out": True}
        except asyncio.CancelledError:
            return {"reply": "", "cancelled": True}
        finally:
            self._active_asks.pop(entry.key, None)

    # -- session_reply --------------------------------------------------------

    async def _handle_reply(self, params: dict) -> dict:
        """Resolve the pending entry for the asking session."""
        replying_session_id = params.get("session_id", "")
        request_id = params.get("request_id", "")
        reply = params.get("reply", "")

        if not request_id:
            return {"error": "request_id is required"}
        if not reply:
            return {"error": "reply is required"}

        pending = self.engine.pending
        entry = pending.get(request_id)

        if not entry:
            return {
                "error": f"No pending request with ID {request_id}. "
                "It may have timed out or already been answered."
            }

        # Verify the pending entry is a session_ask type
        if entry.data.get("type") != "session_ask":
            return {"error": "Request ID does not correspond to a session_ask"}

        # Verify the replying session is the intended target (fail-closed)
        expected_target = entry.data.get("target_session_id", "")
        if replying_session_id != expected_target:
            return {
                "error": f"This question was directed to session "
                f"{expected_target[:8]}, not you ({replying_session_id[:8]})"
            }

        # Resolve — unblocks the asking session
        if pending.resolve(request_id, reply):
            if self.engine.events:
                self.engine.events.log(
                    "session_ask.replied",
                    session_id=replying_session_id,
                    detail={
                        "request_id": request_id,
                        "asking_session": entry.session_id[:8],
                        "reply_length": len(reply),
                    },
                )
            return {"resolved": True}
        else:
            return {"error": "Request already resolved or expired"}

    # -- session_list ---------------------------------------------------------

    async def _handle_list(self, params: dict) -> dict:
        """List sessions visible to the calling session's user."""
        session_id = params.get("session_id", "")
        session = self.sm.sessions.get(session_id)
        if not session:
            return {"error": "Current session not found"}

        user_sessions = self.sm.get_sessions_for_user(session.user_id)
        return {
            "sessions": [
                {
                    "session_id": s.id,
                    "name": s.name,
                    "slot": s.slot,
                    "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                    "is_self": s.id == session_id,
                }
                for s in user_sessions
            ]
        }
