"""Browser extension — web automation + scraping.

Two integration tiers:
- agent-browser CLI: interactive browser automation via Bash (system prompt)
- Scrapling MCP: anti-bot web scraping via gateway tool (MCPServerBase)

Uses agent-browser (https://github.com/vercel-labs/agent-browser) for
AI-optimized interactive web browsing, and Scrapling
(https://github.com/D4Vinci/Scrapling) for data fetching with TLS
fingerprint impersonation and anti-bot bypass.
"""

import asyncio
import logging
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

    def reconfigure(self, config: dict) -> None:
        super().reconfigure(config)
        self._binary = config.get("binary", "agent-browser")

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

        # System prompt for agent-browser CLI (interactive browsing)
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="browser")

        # MCP server for Scrapling (web scraping with anti-bot bypass)
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

        log.info("Browser extension started (binary=%s, scraping=enabled)", self._binary)

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
        return {
            "status": "ok",
            "binary": self._binary,
            "daemon_running": daemon_running,
        }
