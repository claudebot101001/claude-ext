#!/usr/bin/env python3
"""Stealth browser MCP server — anti-detect interactive browsing via Patchright.

Provides interactive browser automation tools with anti-bot detection bypass.
Uses Patchright (undetected Playwright fork) with optional NopeCHA CAPTCHA
solver extension.

Stateful: a single browser instance persists across tool calls within the
MCP server process lifetime. Lazy-launched on first stealth_open.

Async bridge: Patchright is async; MCPServerBase.run() is sync. A background
thread runs the asyncio event loop, and sync handlers dispatch via
run_coroutine_threadsafe.

Gateway-compatible: in gateway mode, all tools consolidate into a single
`stealth_browser(action=..., params={...})` tool.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402

# JS for element enumeration (snapshot)
_SNAPSHOT_JS = (Path(__file__).with_name("stealth_snapshot.js")).read_text(encoding="utf-8")

# Idle timeout before auto-closing browser (seconds)
_DEFAULT_IDLE_TIMEOUT = 300


class StealthBrowserManager:
    """Manages a long-lived Patchright browser instance."""

    def __init__(self, config: dict):
        self._config = config
        self._pw = None
        self._context = None
        self._page = None
        self._refs: dict[str, dict] = {}  # ref_id -> {selector, role, name}
        self._idle_timeout = config.get("idle_timeout", _DEFAULT_IDLE_TIMEOUT)
        self._idle_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._xvfb_proc: subprocess.Popen | None = None

    @property
    def is_running(self) -> bool:
        return self._page is not None

    async def ensure_browser(self, url: str | None = None) -> None:
        """Lazy-launch browser on first use."""
        if self._page is not None:
            if url:
                await self._page.goto(url, wait_until="domcontentloaded")
            self._reset_idle()
            return

        try:
            from patchright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "patchright not installed. Run: pip install patchright && python -m patchright install chromium"
            )

        self._pw = await async_playwright().start()

        args = self._config.get("chromium_args", [])[:]
        nopecha_path = self._resolve_nopecha()

        if nopecha_path:
            args.extend(
                [
                    f"--disable-extensions-except={nopecha_path}",
                    f"--load-extension={nopecha_path}",
                ]
            )

        # Session-specific user data dir to avoid collisions
        session_id = os.environ.get("CLAUDE_EXT_SESSION_ID", "default")
        user_data_dir = f"/tmp/patchright-{session_id}"

        # headless=False needed for extension loading; Xvfb provides virtual display
        # If no extensions, headless=True is fine (Patchright expects boolean)
        use_headless = nopecha_path is None
        headless_val = use_headless

        # Auto-start Xvfb if headless=False and no DISPLAY set
        if not headless_val and not os.environ.get("DISPLAY"):
            self._ensure_xvfb()

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless_val,
            args=args,
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )

        # Use existing or create new page
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        if url:
            await self._page.goto(url, wait_until="domcontentloaded")

        self._reset_idle()

    def _ensure_xvfb(self) -> None:
        """Start Xvfb virtual display if not already running."""
        import shutil

        if not shutil.which("Xvfb"):
            raise RuntimeError("Xvfb not installed. Run: sudo apt install xvfb")
        # Pick a display number unlikely to collide
        display = ":99"
        try:
            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = display
        except Exception as e:
            raise RuntimeError(f"Failed to start Xvfb: {e}")

    def _resolve_nopecha(self) -> str | None:
        """Find NopeCHA extension directory if configured."""
        solver = self._config.get("captcha_solver", "none")
        if solver != "nopecha":
            return None
        # Look in vendor/ next to this file
        vendor_dir = Path(__file__).with_name("vendor")
        if not vendor_dir.exists():
            return None
        # Find nopecha-* directory
        for d in sorted(vendor_dir.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("nopecha"):
                manifest = d / "manifest.json"
                if manifest.exists():
                    # Inject API key if configured
                    api_key = self._config.get("nopecha_api_key", "")
                    if api_key:
                        self._set_nopecha_key(manifest, api_key)
                    return str(d)
        return None

    @staticmethod
    def _set_nopecha_key(manifest_path: Path, api_key: str) -> None:
        """Inject API key into NopeCHA manifest.json nopecha.key field."""
        try:
            data = json.loads(manifest_path.read_text())
            if data.get("nopecha", {}).get("key") != api_key:
                data["nopecha"]["key"] = api_key
                manifest_path.write_text(json.dumps(data, indent="\t"))
        except Exception:
            pass

    def _reset_idle(self) -> None:
        """Reset the idle auto-close timer."""
        if self._idle_handle:
            self._idle_handle.cancel()
        if self._loop and self._idle_timeout > 0:
            self._idle_handle = self._loop.call_later(
                self._idle_timeout, lambda: asyncio.ensure_future(self.cleanup())
            )

    async def snapshot(self) -> str:
        """Enumerate interactive elements and return ref text."""
        if not self._page:
            return "Error: no browser open. Use stealth_open first."

        self._reset_idle()
        result_json = await self._page.evaluate(_SNAPSHOT_JS)
        data = json.loads(result_json)
        self._refs = data.get("refs", {})
        text = data.get("text", "")

        # Add page title header like agent-browser
        title = await self._page.title()
        url = self._page.url
        header = f"{title}\n  {url}" if title else url
        return f"{header}\n{text}" if text else header

    async def click(self, ref: str = "", x: int = 0, y: int = 0) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        if x and y:
            await self._page.mouse.click(x, y)
            return "Done"
        if not ref:
            return "Error: provide 'ref' or 'x'+'y' coordinates."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        await self._page.click(info["selector"])
        return "Done"

    async def fill(self, ref: str, value: str) -> str:
        if not self._page:
            return "Error: no browser open."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        self._reset_idle()
        await self._page.fill(info["selector"], value)
        return "Done"

    async def select_option(self, ref: str, value: str) -> str:
        if not self._page:
            return "Error: no browser open."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        self._reset_idle()
        await self._page.select_option(info["selector"], label=value)
        return "Done"

    async def type_text(self, text: str) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        await self._page.keyboard.type(text)
        return "Done"

    async def press_key(self, key: str) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        await self._page.keyboard.press(key)
        return "Done"

    async def wait_for(self, selector: str | None, timeout: int = 10000) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        if selector:
            await self._page.wait_for_selector(selector, timeout=timeout)
            return f"Selector '{selector}' is visible."
        else:
            await self._page.wait_for_load_state("networkidle", timeout=timeout)
            return "Network idle."

    async def evaluate_js(self, js: str) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        result = await self._page.evaluate(js)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    async def screenshot(self, path: str) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        await self._page.screenshot(path=path)
        return f"Screenshot saved to {path}"

    async def get_url(self) -> str:
        if not self._page:
            return "Error: no browser open."
        return self._page.url

    async def get_title(self) -> str:
        if not self._page:
            return "Error: no browser open."
        return await self._page.title()

    async def get_text(self, selector: str | None = None) -> str:
        if not self._page:
            return "Error: no browser open."
        self._reset_idle()
        if selector:
            el = await self._page.query_selector(selector)
            if not el:
                return f"No element matches: {selector}"
            return await el.inner_text()
        return await self._page.inner_text("body")

    async def goto(self, url: str) -> str:
        if not self._page:
            return "Error: no browser open. Use open first."
        self._reset_idle()
        await self._page.goto(url, wait_until="domcontentloaded")
        title = await self._page.title()
        return f"{title}\n  {self._page.url}"

    async def cleanup(self) -> None:
        """Close browser and release resources."""
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._page = None
        self._refs = {}
        if self._xvfb_proc:
            self._xvfb_proc.terminate()
            self._xvfb_proc = None


class StealthBrowserMCPServer(MCPServerBase):
    name = "stealth_browser"
    gateway_description = (
        "Anti-detect stealth browser with CAPTCHA bypass. action='help' for commands."
    )
    tools = [
        {
            "name": "open",
            "description": "Launch stealth browser and navigate to URL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "goto",
            "description": "Navigate to a different URL (browser must be open).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "snapshot",
            "description": "Get interactive element refs (@e1, @e2...) from current page.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "click",
            "description": "Click an element by ref (e.g. 'e1') or by x,y pixel coordinates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot"},
                    "x": {"type": "integer", "description": "X pixel coordinate"},
                    "y": {"type": "integer", "description": "Y pixel coordinate"},
                },
            },
        },
        {
            "name": "fill",
            "description": "Fill an input field by ref.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot"},
                    "value": {"type": "string", "description": "Value to fill"},
                },
                "required": ["ref", "value"],
            },
        },
        {
            "name": "select",
            "description": "Select an option from a dropdown by ref.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from snapshot"},
                    "value": {
                        "type": "string",
                        "description": "Option label to select",
                    },
                },
                "required": ["ref", "value"],
            },
        },
        {
            "name": "type",
            "description": "Type text at current focus (keyboard input).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "press",
            "description": "Press a key (Enter, Tab, Escape, etc.).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name (Enter, Tab, Escape, ArrowDown, etc.)",
                    },
                },
                "required": ["key"],
            },
        },
        {
            "name": "wait",
            "description": "Wait for a CSS selector to appear or network idle.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to wait for (omit for network idle)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in ms (default: 10000)",
                    },
                },
            },
        },
        {
            "name": "evaluate",
            "description": "Execute JavaScript on the page and return result.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "js": {
                        "type": "string",
                        "description": "JavaScript expression to evaluate",
                    },
                },
                "required": ["js"],
            },
        },
        {
            "name": "screenshot",
            "description": "Take a screenshot of the current page.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to save screenshot (PNG)",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "get_url",
            "description": "Get the current page URL.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_title",
            "description": "Get the current page title.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_text",
            "description": "Get text content of the page or a specific CSS selector.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (omit for full page text)",
                    },
                },
            },
        },
        {
            "name": "close",
            "description": "Close the stealth browser and release resources.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    def __init__(self):
        super().__init__()
        self._config = self._load_config()
        self._manager = StealthBrowserManager(self._config)

        # Background event loop for async Patchright operations
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="stealth-event-loop"
        )
        self._thread.start()
        self._manager._loop = self._loop

        # SIGTERM cleanup
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        self.handlers = {
            "open": self._handle_open,
            "goto": self._handle_goto,
            "snapshot": self._handle_snapshot,
            "click": self._handle_click,
            "fill": self._handle_fill,
            "select": self._handle_select,
            "type": self._handle_type,
            "press": self._handle_press,
            "wait": self._handle_wait,
            "evaluate": self._handle_evaluate,
            "screenshot": self._handle_screenshot,
            "get_url": self._handle_get_url,
            "get_title": self._handle_get_title,
            "get_text": self._handle_get_text,
            "close": self._handle_close,
        }

    def _load_config(self) -> dict:
        """Load stealth config from env or defaults."""
        # Config is passed via env var as JSON by extension.py
        raw = os.environ.get("STEALTH_BROWSER_CONFIG", "{}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _run_loop(self) -> None:
        """Run the asyncio event loop in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_async(self, coro, timeout: int = 60) -> str:
        """Dispatch async coroutine to the background loop and wait."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _handle_sigterm(self, signum, frame):
        """Clean up browser on SIGTERM."""
        if self._manager.is_running:
            future = asyncio.run_coroutine_threadsafe(self._manager.cleanup(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        sys.exit(0)

    # -- handlers (sync wrappers around async manager) -------------------------

    def _handle_open(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: 'url' is required."
        self._run_async(self._manager.ensure_browser(url))
        return self._run_async(self._manager.snapshot())

    def _handle_goto(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: 'url' is required."
        return self._run_async(self._manager.goto(url))

    def _handle_snapshot(self, args: dict) -> str:
        return self._run_async(self._manager.snapshot())

    def _handle_click(self, args: dict) -> str:
        ref = args.get("ref", "")
        x = args.get("x", 0)
        y = args.get("y", 0)
        if not ref and not (x and y):
            return "Error: 'ref' or 'x'+'y' coordinates required."
        return self._run_async(self._manager.click(ref, x, y))

    def _handle_fill(self, args: dict) -> str:
        ref = args.get("ref", "")
        if not ref:
            return "Error: 'ref' is required."
        if "value" not in args:
            return "Error: 'value' is required."
        return self._run_async(self._manager.fill(ref, args["value"]))

    def _handle_select(self, args: dict) -> str:
        ref = args.get("ref", "")
        value = args.get("value", "")
        if not ref:
            return "Error: 'ref' is required."
        if not value:
            return "Error: 'value' is required."
        return self._run_async(self._manager.select_option(ref, value))

    def _handle_type(self, args: dict) -> str:
        text = args.get("text", "")
        if not text:
            return "Error: 'text' is required."
        return self._run_async(self._manager.type_text(text))

    def _handle_press(self, args: dict) -> str:
        key = args.get("key", "")
        if not key:
            return "Error: 'key' is required."
        return self._run_async(self._manager.press_key(key))

    def _handle_wait(self, args: dict) -> str:
        selector = args.get("selector")
        timeout_ms = args.get("timeout", 10000)
        # Allow _run_async ceiling to exceed the Playwright wait timeout
        run_timeout = max(60, timeout_ms // 1000 + 10)
        return self._run_async(self._manager.wait_for(selector, timeout_ms), timeout=run_timeout)

    def _handle_evaluate(self, args: dict) -> str:
        js = args.get("js", "")
        if not js:
            return "Error: 'js' is required."
        return self._run_async(self._manager.evaluate_js(js))

    def _handle_screenshot(self, args: dict) -> str:
        path = args.get("path", "")
        if not path:
            return "Error: 'path' is required."
        return self._run_async(self._manager.screenshot(path))

    def _handle_get_url(self, args: dict) -> str:
        return self._run_async(self._manager.get_url())

    def _handle_get_title(self, args: dict) -> str:
        return self._run_async(self._manager.get_title())

    def _handle_get_text(self, args: dict) -> str:
        selector = args.get("selector")
        return self._run_async(self._manager.get_text(selector))

    def _handle_close(self, args: dict) -> str:
        self._run_async(self._manager.cleanup())
        return "Browser closed."


if __name__ == "__main__":
    StealthBrowserMCPServer().run()
