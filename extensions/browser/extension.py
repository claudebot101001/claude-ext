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
import json
import logging
import os
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
                    "name": "close",
                    "description": "Close stealth browser",
                },
            ],
        )

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
