"""Per-session token usage tracker.

Pure in-memory data structure — no file I/O, no external dependencies.
Updated from delivery callback metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class SessionTokens:
    """Token usage snapshot for a single session."""

    context_window: int = 200000
    last_context_fill: int = 0
    last_output: int = 0
    total_cost_usd: float = 0.0
    compaction_count: int = 0
    prompt_count: int = 0
    last_compact_at_prompt: int = -1
    updated_at: str = ""

    def estimated_fill_pct(self) -> float:
        if not self.context_window:
            return 0.0
        return self.last_context_fill / self.context_window * 100


class ContextTracker:
    """Accumulates token usage per session."""

    def __init__(self):
        self._sessions: dict[str, SessionTokens] = {}

    def get(self, session_id: str) -> SessionTokens | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str) -> SessionTokens:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionTokens()
        return self._sessions[session_id]

    def update_from_stream_usage(self, session_id: str, usage: dict) -> None:
        """Update from a streaming assistant event's message.usage."""
        tokens = self.get_or_create(session_id)
        context_fill = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        if context_fill > 0:
            tokens.last_context_fill = context_fill
        tokens.last_output = usage.get("output_tokens", 0)
        tokens.updated_at = datetime.now(UTC).isoformat()

    def update_from_result(self, session_id: str, metadata: dict) -> None:
        """Update from an is_final delivery metadata dict."""
        tokens = self.get_or_create(session_id)
        tokens.prompt_count += 1
        tokens.total_cost_usd += metadata.get("total_cost_usd", 0) or 0
        tokens.updated_at = datetime.now(UTC).isoformat()

        model_usage = metadata.get("model_usage")
        if model_usage and isinstance(model_usage, dict):
            for model_data in model_usage.values():
                if isinstance(model_data, dict) and "contextWindow" in model_data:
                    tokens.context_window = model_data["contextWindow"]
                    break

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def session_ids(self) -> set[str]:
        return set(self._sessions.keys())

    def to_dict(self, session_id: str) -> dict:
        """Return tracker data as a plain dict for MCP responses."""
        tokens = self.get(session_id)
        if not tokens:
            return {"error": "No token data for this session yet."}
        return {
            "context_window": tokens.context_window,
            "last_context_fill": tokens.last_context_fill,
            "estimated_fill_pct": round(tokens.estimated_fill_pct(), 1),
            "last_output_tokens": tokens.last_output,
            "total_cost_usd": round(tokens.total_cost_usd, 6),
            "prompt_count": tokens.prompt_count,
            "compaction_count": tokens.compaction_count,
        }
