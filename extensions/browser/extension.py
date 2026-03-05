"""Browser extension — web automation + scraping + stealth browsing.

Three integration tiers:
- agent-browser CLI: interactive browser automation via Bash (system prompt)
- Scrapling MCP: anti-bot web scraping via gateway tool (MCPServerBase)
- Patchright stealth: anti-detect interactive browser via MCP gateway (stealth_server)

Uses agent-browser (https://github.com/vercel-labs/agent-browser) for
AI-optimized interactive web browsing, Scrapling
(https://github.com/D4Vinci/Scrapling) for data fetching with TLS
fingerprint impersonation and anti-bot bypass, and Patchright
(https://github.com/AcierP/patchright-python) for undetected browser
automation with optional NopeCHA CAPTCHA solving.
"""

import asyncio
import email
import imaplib
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from core.extension import Extension

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
## Browser Automation (agent-browser CLI)
Use `agent-browser` via Bash for web interaction. Core workflow:
1. `agent-browser open <url>` — navigate to URL
2. `agent-browser snapshot -i` — get interactive element refs (@e1, @e2...)
3. `agent-browser click/fill/select @ref` — interact using refs
4. Re-snapshot after actions (refs invalidate on DOM change)

Chain with `&&` when intermediate output isn't needed.
Key commands: open, snapshot, click, fill, select, type, press, wait, screenshot, pdf, get text/url/title
Run `agent-browser --help` for full command list and options."""


def _extract_verification(body: str) -> dict:
    """Extract verification codes and links from an email body."""
    # Find verification codes (4-8 digits near code-related keywords)
    codes = re.findall(r"(?:code|pin|otp|verification)[^\d]{0,20}(\d{4,8})", body, re.IGNORECASE)
    if not codes:
        # Fallback: standalone 4-8 digit numbers
        codes = re.findall(r"\b(\d{4,8})\b", body)
        # Filter likely non-codes (years)
        codes = [c for c in codes if not (1900 <= int(c) <= 2100)]
    # Find verification links
    urls = re.findall(r"https?://[^\s<>\"]+", body)
    verify_keywords = ("verify", "confirm", "activate", "token", "validate", "auth")
    links = [u for u in urls if any(kw in u.lower() for kw in verify_keywords)]
    return {"codes": codes[:5], "links": links[:10]}


class ExtensionImpl(Extension):
    name = "browser"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._binary = config.get("binary", "agent-browser")
        self._stealth_config = config.get("stealth", {})

    def reconfigure(self, config: dict) -> None:
        super().reconfigure(config)
        self._binary = config.get("binary", "agent-browser")
        self._stealth_config = config.get("stealth", {})

    @property
    def sm(self):
        return self.engine.session_manager

    async def start(self) -> None:
        if not shutil.which(self._binary):
            log.warning(
                "Browser extension: '%s' not in PATH. "
                "Install: npm install -g agent-browser && agent-browser install",
                self._binary,
            )

        # Tier 1: System prompt for agent-browser CLI (interactive browsing)
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="browser")

        # Tier 2: MCP server for Scrapling (web scraping with anti-bot bypass)
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server(
            "browser",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {},
            },
            tools=[
                {
                    "name": "scrape",
                    "description": "HTTP fetch with TLS fingerprint impersonation (fast, no browser)",
                },
                {
                    "name": "scrape_stealth",
                    "description": "Stealth browser fetch with anti-bot bypass (Cloudflare etc.)",
                },
                {
                    "name": "scrape_extract",
                    "description": "Extract structured data via CSS selectors",
                },
            ],
        )

        # Tier 3: MCP server for Patchright stealth browser (anti-detect interactive)
        stealth_enabled = self._stealth_config.get("enabled", True)
        if stealth_enabled:
            self._register_stealth_server()
            # Register bridge handler for vault credential retrieval from stealth MCP
            if hasattr(self.engine, "bridge") and self.engine.bridge:
                self.engine.bridge.add_handler(self._bridge_handler)

        stealth_status = "enabled" if stealth_enabled else "disabled"
        log.info(
            "Browser extension started (binary=%s, scraping=enabled, stealth=%s)",
            self._binary,
            stealth_status,
        )

    def _register_stealth_server(self) -> None:
        """Register the Patchright stealth browser MCP server."""
        stealth_script = str(Path(__file__).with_name("stealth_server.py"))
        self.sm.register_mcp_server(
            "stealth_browser",
            {
                "command": sys.executable,
                "args": [stealth_script],
                "env": {
                    # Strip secrets before serializing to env var (/proc visible).
                    # API keys (e.g. nopecha_api_key) must be delivered via
                    # bridge RPC at runtime, not through env vars.
                    "STEALTH_BROWSER_CONFIG": json.dumps(
                        {
                            k: v
                            for k, v in self._stealth_config.items()
                            if "key" not in k.lower()
                            and "secret" not in k.lower()
                            and "password" not in k.lower()
                            and k != "proxy"
                        }
                    ),
                    "DISPLAY": os.environ.get("DISPLAY", ":99"),
                },
            },
            tools=[
                {
                    "name": "open",
                    "description": "Launch stealth browser and navigate to URL",
                },
                {
                    "name": "goto",
                    "description": "Navigate to a different URL",
                },
                {
                    "name": "snapshot",
                    "description": "Get interactive element refs (@e1, @e2...)",
                },
                {
                    "name": "click",
                    "description": "Click element by ref",
                },
                {
                    "name": "fill",
                    "description": "Fill input by ref",
                },
                {
                    "name": "select",
                    "description": "Select dropdown option by ref",
                },
                {
                    "name": "type",
                    "description": "Type text at focus",
                },
                {
                    "name": "press",
                    "description": "Press key (Enter, Tab, etc.)",
                },
                {
                    "name": "wait",
                    "description": "Wait for selector or network idle",
                },
                {
                    "name": "evaluate",
                    "description": "Execute JavaScript on page",
                },
                {
                    "name": "screenshot",
                    "description": "Take screenshot",
                },
                {
                    "name": "get_url",
                    "description": "Get current URL",
                },
                {
                    "name": "get_title",
                    "description": "Get page title",
                },
                {
                    "name": "get_text",
                    "description": "Get page text or element text",
                },
                {
                    "name": "upload",
                    "description": "Upload file to input element",
                },
                {
                    "name": "download",
                    "description": "Download file via click",
                },
                {
                    "name": "switch_tab",
                    "description": "Switch to browser tab by index",
                },
                {
                    "name": "switch_frame",
                    "description": "Switch to iframe or main frame",
                },
                {
                    "name": "add_auth_domain",
                    "description": "Add domain to auth skip list",
                },
                {
                    "name": "create_profile",
                    "description": "Create a fingerprint profile",
                },
                {
                    "name": "list_profiles",
                    "description": "List fingerprint profiles",
                },
                {
                    "name": "delete_profile",
                    "description": "Delete a fingerprint profile",
                },
                {
                    "name": "close",
                    "description": "Close stealth browser",
                },
                {
                    "name": "check_email",
                    "description": "Search INBOX for verification emails",
                },
                {
                    "name": "read_email",
                    "description": "Read email and extract verification codes/links",
                },
            ],
        )

    def _imap_connect(self) -> tuple[imaplib.IMAP4_SSL, dict]:
        """Connect to IMAP using credentials from vault. Returns (conn, creds_dict)."""
        vault_key = self._stealth_config.get("email", {}).get("vault_key", "browser/email/imap")
        vault = self.engine.services.get("vault")
        if not vault:
            raise RuntimeError("Vault not enabled — cannot retrieve email credentials")
        raw = vault.get(vault_key)
        if not raw:
            raise RuntimeError(f"Email credentials not found in vault key: {vault_key}")
        creds = json.loads(raw)
        conn = imaplib.IMAP4_SSL(creds["host"], int(creds.get("port", 993)), timeout=30)
        conn.login(creds["username"], creds["password"])
        return conn, creds

    def _imap_search(
        self,
        sender: str | None,
        subject: str | None,
        after: str | None,
        limit: int,
    ) -> list[dict]:
        """Search INBOX and return message summaries (blocking)."""
        conn = None
        try:
            conn, _ = self._imap_connect()
            conn.select("INBOX", readonly=True)
            criteria = []
            if sender:
                # Strip double quotes to prevent IMAP search injection
                safe_sender = sender.replace('"', "")
                criteria.append(f'FROM "{safe_sender}"')
            if subject:
                safe_subject = subject.replace('"', "")
                criteria.append(f'SUBJECT "{safe_subject}"')
            if after:
                from datetime import datetime

                dt = datetime.fromisoformat(after)
                criteria.append(f'SINCE "{dt.strftime("%d-%b-%Y")}"')
            search_str = " ".join(criteria) if criteria else "ALL"
            _, data = conn.search(None, search_str)
            msg_ids = data[0].split()
            msg_ids = msg_ids[-limit:]  # Most recent N
            results = []
            for mid in reversed(msg_ids):
                _, msg_data = conn.fetch(mid, "(RFC822.HEADER BODY.PEEK[TEXT]<0.500>)")
                if not msg_data or not msg_data[0]:
                    continue
                header_bytes = msg_data[0][1] if msg_data[0] else b""
                snippet = ""
                if len(msg_data) > 2 and msg_data[1]:
                    snippet = msg_data[1][1].decode("utf-8", errors="replace")[:200]
                msg = email.message_from_bytes(header_bytes)
                results.append(
                    {
                        "id": mid.decode(),
                        "subject": str(msg.get("Subject", "")),
                        "sender": str(msg.get("From", "")),
                        "date": str(msg.get("Date", "")),
                        "snippet": snippet.strip(),
                    }
                )
            return results
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    def _imap_read(self, message_id: str) -> dict:
        """Read a full message by sequence number (blocking)."""
        conn = None
        try:
            conn, _ = self._imap_connect()
            conn.select("INBOX", readonly=True)
            _, msg_data = conn.fetch(message_id.encode(), "(RFC822)")
            if not msg_data or not msg_data[0]:
                return {"error": f"Message {message_id} not found"}
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
                    elif ct == "text/html" and not body:
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            verification = _extract_verification(body)
            return {
                "subject": str(msg.get("Subject", "")),
                "sender": str(msg.get("From", "")),
                "body": body,
                "codes": verification["codes"],
                "links": verification["links"],
            }
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    async def _bridge_handler(self, method: str, params: dict):
        """Handle bridge RPC calls from stealth browser MCP server."""
        if method == "stealth_vault_retrieve":
            vault = self.engine.services.get("vault")
            if not vault:
                return {"error": "Vault not enabled"}
            key = params.get("key", "")
            if not key:
                return {"error": "Missing 'key' parameter"}
            result = vault.get(key)
            if result is None:
                return {"error": f"Key not found: {key}"}
            return {"value": result}
        if method == "stealth_get_proxy":
            proxy_cfg = self._stealth_config.get("proxy", {})
            if not isinstance(proxy_cfg, dict):
                return {"server": None}
            server = proxy_cfg.get("server")
            # If proxy has a vault_key, fetch credentials from vault
            vault_key = proxy_cfg.get("vault_key")
            if vault_key:
                vault = self.engine.services.get("vault")
                if vault:
                    creds = vault.get(vault_key)
                    if creds:
                        return {"server": server, "credentials": creds}
            return {"server": server}
        if method == "stealth_email_search":
            sender = params.get("sender")
            subject = params.get("subject")
            after = params.get("after")
            limit = params.get("limit", 5)
            try:
                loop = asyncio.get_event_loop()
                results = await loop.run_in_executor(
                    None, self._imap_search, sender, subject, after, limit
                )
                return {"messages": results}
            except Exception as exc:
                return {"error": str(exc)}
        if method == "stealth_email_read":
            message_id = params.get("message_id")
            if not message_id:
                return {"error": "Missing 'message_id' parameter"}
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, self._imap_read, str(message_id))
                return result
            except Exception as exc:
                return {"error": str(exc)}
        return None  # Not our method — let other handlers try

    async def stop(self) -> None:
        log.info("Browser extension stopped.")

    async def health_check(self) -> dict:
        found = shutil.which(self._binary) is not None
        if not found:
            return {
                "status": "degraded",
                "detail": f"'{self._binary}' not found in PATH",
            }
        # Quick daemon probe
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "get",
                "url",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
            daemon_running = proc.returncode == 0
        except (TimeoutError, FileNotFoundError, OSError):
            daemon_running = False

        stealth_enabled = self._stealth_config.get("enabled", True)
        patchright_available = False
        if stealth_enabled:
            try:
                import importlib

                importlib.import_module("patchright")
                patchright_available = True
            except ImportError:
                pass

        return {
            "status": "ok",
            "binary": self._binary,
            "daemon_running": daemon_running,
            "stealth_enabled": stealth_enabled,
            "patchright_available": patchright_available,
        }
