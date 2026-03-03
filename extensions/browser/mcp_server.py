#!/usr/bin/env python3
"""Browser extension MCP server — web scraping via Scrapling.

Provides three scraping tools via MCPServerBase (gateway-compatible).
Uses Scrapling's Fetcher/StealthyFetcher for HTTP and stealth requests.
Browser-based fetching (DynamicFetcher) not included — use agent-browser
CLI via Bash for interactive browser automation instead.

Zero bridge RPC — all operations are self-contained in this process.
"""

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class BrowserMCPServer(MCPServerBase):
    name = "browser"
    gateway_description = (
        "Web scraping with anti-bot bypass (fetch/stealth/extract). action='help' for details."
    )
    tools = [
        {
            "name": "scrape",
            "description": (
                "Fetch a URL via HTTP with TLS fingerprint impersonation. "
                "Fast, no browser needed. Suitable for low-mid protection sites."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to extract specific elements",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text", "html"],
                        "description": "Output format (default: markdown)",
                    },
                    "impersonate": {
                        "type": "string",
                        "description": "Browser to impersonate for TLS fingerprint (default: chrome)",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "scrape_stealth",
            "description": (
                "Fetch a URL using stealth browser with anti-bot bypass. "
                "Handles Cloudflare, DataDome, etc. Slower but defeats protection."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to extract specific elements",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text", "html"],
                        "description": "Output format (default: markdown)",
                    },
                    "solve_cloudflare": {
                        "type": "boolean",
                        "description": "Attempt to solve Cloudflare challenge (default: false)",
                    },
                    "wait": {
                        "type": "number",
                        "description": "Seconds to wait after page load (default: 0)",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "scrape_extract",
            "description": (
                "Extract structured data from a URL using CSS selectors. "
                "Returns matching elements as a list. Uses HTTP fetcher."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch",
                    },
                    "selectors": {
                        "type": "object",
                        "description": (
                            "Named CSS selectors to extract. "
                            'E.g. {"title": "h1", "prices": ".price::text"}'
                        ),
                    },
                },
                "required": ["url", "selectors"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self.handlers = {
            "scrape": self._handle_scrape,
            "scrape_stealth": self._handle_scrape_stealth,
            "scrape_extract": self._handle_scrape_extract,
        }

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _extract_content(response, selector: str | None, fmt: str) -> str:
        """Extract content from a Scrapling response."""
        if selector:
            elements = response.css(selector)
            if not elements:
                return f"No elements matched selector: {selector}"
            parts = []
            for el in elements:
                if fmt == "html":
                    parts.append(el.html_content or "")
                else:
                    parts.append(el.get_all_text() or "")
            return "\n".join(parts)

        if fmt == "html":
            return response.html_content or ""
        # text and markdown: get cleaned text from body
        body = response.css("body")
        if body:
            return body[0].get_all_text() or ""
        return response.get_all_text() or ""

    # -- handlers --------------------------------------------------------------

    def _handle_scrape(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: 'url' is required."

        selector = args.get("selector")
        fmt = args.get("format", "markdown")
        impersonate = args.get("impersonate", "chrome")

        try:
            from scrapling import Fetcher

            fetcher = Fetcher()
            response = fetcher.get(url, stealthy_headers=True, impersonate=impersonate)

            if response.status != 200:
                return f"HTTP {response.status} for {url}"

            return self._extract_content(response, selector, fmt)
        except Exception as e:
            return f"Error: {e}"

    def _handle_scrape_stealth(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: 'url' is required."

        selector = args.get("selector")
        fmt = args.get("format", "markdown")
        solve_cf = args.get("solve_cloudflare", False)
        wait = args.get("wait", 0)

        try:
            from scrapling import StealthyFetcher

            fetcher = StealthyFetcher()
            response = fetcher.fetch(
                url,
                headless=True,
                solve_cloudflare=solve_cf,
                wait=wait,
                network_idle=True,
            )

            if response.status != 200:
                return f"HTTP {response.status} for {url}"

            return self._extract_content(response, selector, fmt)
        except Exception as e:
            return f"Error: {e}"

    def _handle_scrape_extract(self, args: dict) -> str:
        url = args.get("url", "")
        selectors = args.get("selectors", {})
        if not url:
            return "Error: 'url' is required."
        if not selectors:
            return "Error: 'selectors' is required."

        try:
            from scrapling import Fetcher

            fetcher = Fetcher()
            response = fetcher.get(url, stealthy_headers=True)

            if response.status != 200:
                return f"HTTP {response.status} for {url}"

            results = {}
            for name, sel in selectors.items():
                elements = response.css(sel)
                results[name] = [el.get_all_text() or "" for el in elements]

            # Format as readable output
            lines = []
            for name, values in results.items():
                lines.append(f"## {name} ({len(values)} match{'es' if len(values) != 1 else ''})")
                for v in values[:50]:  # cap at 50
                    lines.append(f"  - {v}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


if __name__ == "__main__":
    BrowserMCPServer().run()
