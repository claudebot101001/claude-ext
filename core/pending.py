"""Generic async request/response registry.

One side calls ``register()`` + ``await wait()`` to block until a response
arrives.  Another side calls ``resolve()`` to deliver the response.

Used by ask_user (and potentially future PM/Worker orchestration) without
importing any extension code.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class PendingEntry:
    key: str  # 16-char hex identifier
    session_id: str  # associated session
    data: dict  # extension-defined payload (question, options, etc.)
    future: asyncio.Future = field(repr=False)
    timeout: float = 300.0


class PendingStore:
    """In-memory store for pending request/response pairs."""

    def __init__(self):
        self._entries: dict[str, PendingEntry] = {}

    def register(
        self,
        session_id: str,
        data: dict | None = None,
        timeout: float = 300.0,
    ) -> PendingEntry:
        """Create a new pending entry and return it.

        The caller should then ``await wait(entry.key)`` to block for the
        response.
        """
        key = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        entry = PendingEntry(
            key=key,
            session_id=session_id,
            data=data or {},
            future=future,
            timeout=timeout,
        )
        self._entries[key] = entry
        return entry

    async def wait(self, key: str):
        """Block until the entry is resolved, times out, or is cancelled.

        Returns the resolved value.  Raises ``TimeoutError`` or
        ``asyncio.CancelledError``.  Always cleans up the entry.
        """
        entry = self._entries.get(key)
        if not entry:
            raise KeyError(f"No pending entry with key {key}")
        try:
            return await asyncio.wait_for(entry.future, timeout=entry.timeout)
        finally:
            self._entries.pop(key, None)

    def resolve(self, key: str, value) -> bool:
        """Deliver a response to a pending entry.

        Returns True if the entry existed and was resolved, False otherwise.
        """
        entry = self._entries.get(key)
        if not entry or entry.future.done():
            return False
        entry.future.set_result(value)
        return True

    def cancel_for_session(self, session_id: str) -> int:
        """Cancel all pending entries for a session.  Returns count cancelled."""
        cancelled = 0
        for _key, entry in list(self._entries.items()):
            if entry.session_id == session_id and not entry.future.done():
                entry.future.cancel()
                cancelled += 1
        return cancelled

    def get(self, key: str) -> PendingEntry | None:
        return self._entries.get(key)

    def get_for_session(self, session_id: str) -> PendingEntry | None:
        """Return the first pending entry for a session, or None."""
        for entry in self._entries.values():
            if entry.session_id == session_id:
                return entry
        return None
