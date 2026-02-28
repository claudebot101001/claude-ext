"""Status gathering - subscription usage, auth info, session stats."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_API = "https://api.anthropic.com/api/oauth/usage"


def _read_access_token() -> str | None:
    """Read OAuth access token from Claude Code credentials."""
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        return data["claudeAiOauth"]["accessToken"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        log.warning("Could not read Claude credentials from %s", CREDENTIALS_PATH)
        return None


async def get_auth_info() -> dict:
    """Get auth status via `claude auth status`."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return json.loads(stdout.decode())
    except Exception as e:
        log.warning("Failed to get auth info: %s", e)
        return {}


async def get_usage() -> dict:
    """Query subscription usage (5h + 7d windows) via OAuth API."""
    token = _read_access_token()
    if not token:
        return {"error": "No access token found"}

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s",
            "-H", f"Authorization: Bearer {token}",
            "-H", "anthropic-beta: oauth-2025-04-20",
            "-H", "User-Agent: claude-code/2.1.56",
            USAGE_API,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return json.loads(stdout.decode())
    except Exception as e:
        log.warning("Failed to query usage API: %s", e)
        return {"error": str(e)}


def format_status(auth: dict, usage: dict, session: dict | None = None) -> str:
    """Format all status info into a readable message."""
    lines = []

    # --- Auth ---
    lines.append("-- Auth --")
    if auth.get("loggedIn"):
        lines.append(f"  Account:  {auth.get('email', 'N/A')}")
        lines.append(f"  Plan:     {auth.get('subscriptionType', 'N/A')}")
    else:
        lines.append("  Not logged in")

    # --- Usage quota ---
    lines.append("")
    lines.append("-- Usage Quota --")
    if "error" in usage:
        lines.append(f"  Error: {usage['error']}")
    else:
        for window_key, label in [("five_hour", "5h window"), ("seven_day", "7d window")]:
            window = usage.get(window_key) or {}
            util = window.get("utilization")
            resets_at = window.get("resets_at")
            if util is not None:
                bar = _progress_bar(util)
                reset_str = ""
                if resets_at:
                    reset_str = f"  resets {relative_time(resets_at)}"
                lines.append(f"  {label}: {bar} {util:.0f}%{reset_str}")

        # Extra usage (overage billing)
        extra = usage.get("extra_usage") or {}
        if extra.get("is_enabled"):
            used = extra.get("used_credits", 0)
            limit = extra.get("monthly_limit", 0)
            util = extra.get("utilization", 0)
            lines.append(f"  Overage:  ${used:.0f}/${limit} ({util:.1f}%)")

    # --- Session ---
    if session:
        lines.append("")
        lines.append("-- Session --")
        if session.get("session_id"):
            lines.append(f"  ID:       {session['session_id'][:12]}...")
        if session.get("num_turns") is not None:
            lines.append(f"  Turns:    {session['num_turns']}")
        if session.get("total_cost_usd") is not None:
            lines.append(f"  Cost:     ${session['total_cost_usd']:.4f}")
        if session.get("duration_ms") is not None:
            secs = session["duration_ms"] / 1000
            lines.append(f"  Duration: {secs:.1f}s")

    return "\n".join(lines)


def _progress_bar(percent: float, width: int = 10) -> str:
    filled = int(percent * width / 100)
    filled = min(filled, width)
    return "\u2593" * filled + "\u2591" * (width - filled)


def relative_time(iso_str: str) -> str:
    try:
        reset_dt = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = reset_dt - now
        total_mins = int(delta.total_seconds() / 60)
        if total_mins < 0:
            return "now"
        if total_mins < 60:
            return f"in {total_mins}m"
        hours = total_mins // 60
        mins = total_mins % 60
        if hours < 24:
            return f"in {hours}h{mins}m"
        days = hours // 24
        return f"in {days}d{hours % 24}h"
    except (ValueError, TypeError):
        return ""
