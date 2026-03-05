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
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Ensure DISPLAY is set for headless Linux (Xvfb must be running externally or will be started)
if not os.environ.get("DISPLAY"):
    # Check if Xvfb :99 is already running
    _lock = "/tmp/.X99-lock"
    if os.path.exists(_lock):
        try:
            with open(_lock) as _f:
                _pid = int(_f.read().strip())
            os.kill(_pid, 0)
            os.environ["DISPLAY"] = ":99"
        except (ValueError, OSError, ProcessLookupError):
            pass

_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402

# JS for element enumeration (snapshot)
_SNAPSHOT_JS = (Path(__file__).with_name("stealth_snapshot.js")).read_text(encoding="utf-8")

# JS for fingerprint evasion (WebGL, WebRTC, hardware)
_EVASION_JS = (Path(__file__).with_name("stealth_evasions.js")).read_text(encoding="utf-8")

# Idle timeout before auto-closing browser (seconds)
_DEFAULT_IDLE_TIMEOUT = 300

# Pre-compiled regex for auth path segment-boundary matching
_AUTH_PATH_RE = re.compile(r"(?:^|/)(?:oauth|authorize|login|signin|saml|sso)(?:/|$|\?)")


class StealthBrowserManager:
    """Manages a long-lived Patchright browser instance."""

    def __init__(self, config: dict, bridge=None):
        self._config = config
        self._bridge = bridge
        self._pw = None
        self._context = None
        self._page = None
        self._frame = None  # Active frame locator (None = main frame)
        self._refs: dict[str, dict] = {}  # ref_id -> {selector, role, name}
        self._last_dialog: dict | None = None
        self._pages: list = []  # Track all pages (tabs/popups)
        self._idle_timeout = config.get("idle_timeout", _DEFAULT_IDLE_TIMEOUT)
        self._idle_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._xvfb_proc: subprocess.Popen | None = None
        # Proxy server URL is NOT passed via env (may contain credentials).
        # It will be fetched via bridge RPC at browser launch time.
        self._proxy_server = None
        self._proxy_vault_key = None
        self._active_profile: dict | None = None  # Loaded profile fingerprint config
        self._profiles_base = Path(
            config.get("profiles_dir", "") or os.path.expanduser("~/.claude-ext/browser_profiles")
        )
        self._auth_skip_domains: set[str] = set(
            config.get(
                "auth_skip_domains",
                [
                    "accounts.google.com",
                    "accounts.youtube.com",
                    "github.com",
                    "api.github.com",
                    "appleid.apple.com",
                    "login.microsoftonline.com",
                    "login.live.com",
                    "www.facebook.com",
                    "m.facebook.com",
                    "login.yahoo.com",
                    "id.atlassian.com",
                ],
            )
        )

    @property
    def is_running(self) -> bool:
        return self._page is not None

    async def ensure_browser(
        self,
        url: str | None = None,
        profile: str | None = None,
        proxy_override: str | None = None,
    ) -> None:
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

        # Load profile if specified
        if profile:
            loaded = self._load_profile(profile)
            if loaded:
                self._active_profile = loaded

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

        # Persistent profile directory for browser data
        profiles_dir = Path(
            self._config.get("profiles_dir", "")
            or os.path.expanduser("~/.claude-ext/browser_profiles")
        )
        session_id = re.sub(
            r"[^a-zA-Z0-9_-]", "", os.environ.get("CLAUDE_EXT_SESSION_ID", "default")
        )
        if profile:
            profile_name = re.sub(r"[^a-zA-Z0-9_-]", "", profile)
            user_data_dir = str(profiles_dir / profile_name)
        else:
            user_data_dir = str(profiles_dir / session_id)
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        # headless=False needed for extension loading; Xvfb provides virtual display
        # If no extensions, headless=True is fine (Patchright expects boolean)
        use_headless = nopecha_path is None
        headless_val = use_headless

        # Auto-start Xvfb if headless=False and no DISPLAY set
        if not headless_val and not os.environ.get("DISPLAY"):
            self._ensure_xvfb()

        # Proxy configuration (per-call override > bridge RPC > none)
        proxy_config = None
        proxy_server = proxy_override
        if not proxy_server and self._bridge:
            try:
                proxy_data = self._bridge.call("stealth_get_proxy", {})
                if isinstance(proxy_data, dict) and proxy_data.get("server"):
                    proxy_server = proxy_data["server"]
            except Exception as exc:
                log.debug("Failed to fetch proxy config via bridge: %s", exc)
        if proxy_server:
            proxy_config = {"server": proxy_server}

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless_val,
            args=args,
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
            proxy=proxy_config,
        )

        # Use existing page (persistent context always has one)
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        # Inject fingerprint evasions via route interception
        # (Patchright blocks addInitScript and CDP Page.addScriptToEvaluateOnNewDocument)
        config_json = json.dumps(self._get_stealth_config()).replace("</", "<\\/")
        config_tag = f"<script>window.__STEALTH_CONFIG__={config_json};</script>"
        _evasion_tag = f"{config_tag}<script>{_EVASION_JS}</script>".encode()

        _mgr_ref = self  # capture for closure (access _is_auth_url)

        async def _inject_evasions(route):
            try:
                # Skip if current page or request is on an auth domain —
                # Google GAIA breaks under any route interception including continue_()
                from urllib.parse import urlparse

                page_parsed = urlparse(_mgr_ref._page.url if _mgr_ref._page else "")
                page_host = page_parsed.hostname or ""
                if _mgr_ref._is_auth_url(page_host, page_parsed.path):
                    await route.fallback()
                    return

                req_parsed = urlparse(route.request.url)
                req_host = req_parsed.hostname or ""
                if _mgr_ref._is_auth_url(req_host, req_parsed.path):
                    await route.fallback()
                    return

                # Only modify document/iframe HTML responses
                if route.request.resource_type not in ("document", "iframe"):
                    await route.fallback()
                    return

                resp = await route.fetch()
                ct = resp.headers.get("content-type", "")
                if "text/html" in ct:
                    body = await resp.body()
                    if b"<head>" in body:
                        body = body.replace(b"<head>", b"<head>" + _evasion_tag, 1)
                    elif b"<head " in body:
                        idx = body.index(b"<head ")
                        close = body.index(b">", idx)
                        body = body[: close + 1] + _evasion_tag + body[close + 1 :]
                    await route.fulfill(response=resp, body=body)
                else:
                    await route.fulfill(response=resp)
            except Exception as exc:
                log.debug("Evasion route handler error: %s", exc)
                try:
                    await route.fallback()
                except Exception:
                    pass

        self._inject_evasions = _inject_evasions
        await self._page.route("**/*", _inject_evasions)

        # Track pages list
        self._pages = list(self._context.pages)

        # Dialog handler: auto-accept and store last dialog info
        def _on_dialog(dialog):
            self._last_dialog = {
                "type": dialog.type,
                "message": dialog.message,
            }
            asyncio.ensure_future(dialog.accept())

        self._page.on("dialog", _on_dialog)

        # Popup handler: track new pages/tabs
        def _on_page(page):
            if page not in self._pages:
                self._pages.append(page)

        self._context.on("page", _on_page)

        if url:
            await self._page.goto(url, wait_until="domcontentloaded")

        self._reset_idle()

    def _ensure_xvfb(self) -> None:
        """Start Xvfb virtual display if not already running."""
        import shutil
        import time

        if not shutil.which("Xvfb"):
            raise RuntimeError("Xvfb not installed. Run: sudo apt install xvfb")

        # Try to reuse an existing Xvfb on :99 first
        display = ":99"
        lock_file = f"/tmp/.X{display[1:]}-lock"
        if os.path.exists(lock_file):
            # Verify the Xvfb process is still alive
            try:
                with open(lock_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)  # Check if alive
                os.environ["DISPLAY"] = display
                return  # Reuse existing Xvfb
            except (ValueError, OSError, ProcessLookupError):
                # Stale lock file, clean up and start fresh
                try:
                    os.unlink(lock_file)
                except OSError:
                    pass

        try:
            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = display
            # Wait for Xvfb to be ready
            time.sleep(0.5)
            if self._xvfb_proc.poll() is not None:
                raise RuntimeError(f"Xvfb exited with code {self._xvfb_proc.returncode}")
        except RuntimeError:
            raise
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

    def _get_stealth_config(self) -> dict:
        """Build stealth config dict from active profile or defaults."""
        defaults = {
            "canvas_seed": 42,
            "audio_seed": 42,
            "webgl_vendor": "Intel Inc.",
            "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92), OpenGL 4.6)",
            "hardware_concurrency": 8,
            "device_memory": 8,
            "screen_width": 1920,
            "screen_height": 1080,
            "timezone": "America/New_York",
            "locale": "en-US",
        }
        if self._active_profile and "fingerprint" in self._active_profile:
            fp = self._active_profile["fingerprint"]
            for key in defaults:
                if key in fp:
                    defaults[key] = fp[key]
        return defaults

    def _load_profile(self, name: str) -> dict | None:
        """Load a profile JSON from disk."""
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
        profile_path = self._profiles_base / safe_name / "profile.json"
        if not profile_path.exists():
            return None
        try:
            return json.loads(profile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def create_profile(self, name: str, overrides: dict | None = None) -> str:
        """Create a new fingerprint profile with deterministic defaults."""
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
        if not safe_name:
            return "Error: invalid profile name."
        profile_dir = self._profiles_base / safe_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Seed from profile name for deterministic fingerprint generation
        name_hash = int(hashlib.sha256(safe_name.encode()).hexdigest()[:8], 16)
        rng_seed = name_hash & 0xFFFFFFFF

        # Generate deterministic values from seed
        import random

        rng = random.Random(rng_seed)
        concurrency_options = [4, 8, 12, 16]
        memory_options = [4, 8, 16]
        screen_options = [
            (1920, 1080),
            (2560, 1440),
            (1366, 768),
            (1536, 864),
            (1440, 900),
        ]
        screen = rng.choice(screen_options)
        tz_options = [
            "America/New_York",
            "America/Chicago",
            "America/Denver",
            "America/Los_Angeles",
            "Europe/London",
            "Europe/Paris",
        ]

        profile = {
            "name": safe_name,
            "fingerprint": {
                "canvas_seed": rng.randint(1, 2**31),
                "audio_seed": rng.randint(1, 2**31),
                "webgl_vendor": "Intel Inc.",
                "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92), OpenGL 4.6)",
                "hardware_concurrency": rng.choice(concurrency_options),
                "device_memory": rng.choice(memory_options),
                "screen_width": screen[0],
                "screen_height": screen[1],
                "timezone": rng.choice(tz_options),
                "locale": "en-US",
            },
            "proxy_server": None,
            "proxy_vault_key": None,
        }

        # Apply overrides
        if overrides:
            for key, val in overrides.items():
                if key in profile["fingerprint"]:
                    profile["fingerprint"][key] = val
                elif key in ("proxy_vault_key",):
                    profile[key] = val
                elif key == "proxy_server":
                    # SECURITY: strip credentials from proxy_server
                    from urllib.parse import urlparse

                    parsed = urlparse(val)
                    if parsed.username or parsed.password:
                        return "Error: proxy_server must not contain credentials. Use proxy_vault_key instead."
                    profile["proxy_server"] = val

        profile_path = profile_dir / "profile.json"
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        return f"Profile '{safe_name}' created."

    def list_profiles(self) -> list[str]:
        """List all profile names."""
        if not self._profiles_base.exists():
            return []
        return sorted(
            d.name
            for d in self._profiles_base.iterdir()
            if d.is_dir() and (d / "profile.json").exists()
        )

    def delete_profile(self, name: str) -> str:
        """Delete a profile directory."""
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
        if not safe_name:
            return "Error: invalid profile name."
        profile_dir = self._profiles_base / safe_name
        if not profile_dir.exists():
            return f"Error: profile '{safe_name}' not found."
        import shutil

        shutil.rmtree(profile_dir)
        return f"Profile '{safe_name}' deleted."

    def _is_auth_url(self, hostname: str, path: str = "") -> bool:
        """Check if a URL is an authentication-related URL."""
        if not hostname:
            return False
        # 1. Exact or subdomain match in config list
        for d in self._auth_skip_domains:
            if hostname == d or hostname.endswith("." + d):
                return True
        # 2. Hostname prefix heuristic
        _auth_prefixes = ("login.", "auth.", "signin.", "sso.", "accounts.", "id.")
        if any(hostname.startswith(p) for p in _auth_prefixes):
            return True
        # 3. Path pattern heuristic — segment-boundary matching
        # Only match at path segment boundaries (/ delimited) to avoid false
        # positives like "/blogindex" matching "/login"
        if path:
            if _AUTH_PATH_RE.search(path):
                return True
        return False

    def add_auth_domain(self, domain: str) -> str:
        """Add a domain to the auth skip list at runtime."""
        domain = domain.strip().lower()
        if not domain:
            return "Error: domain is required."
        self._auth_skip_domains.add(domain)
        return f"Added '{domain}' to auth skip list."

    def _fetch_credential(self, key: str) -> str | None:
        """Retrieve a credential from the vault via bridge RPC."""
        if not self._bridge:
            return None
        try:
            result = self._bridge.call("stealth_vault_retrieve", {"key": key})
            if isinstance(result, dict) and "value" in result:
                return result["value"]
            return None
        except Exception as exc:
            log.debug("Failed to fetch credential '%s': %s", key, exc)
            return None

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

    def _locator(self, selector: str):
        """Return a locator scoped to active frame or page."""
        if self._frame:
            return self._frame.locator(selector)
        return self._page.locator(selector)

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
        await self._locator(info["selector"]).click()
        return "Done"

    async def fill(self, ref: str, value: str) -> str:
        if not self._page:
            return "Error: no browser open."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        self._reset_idle()
        await self._locator(info["selector"]).fill(value)
        return "Done"

    async def select_option(self, ref: str, value: str) -> str:
        if not self._page:
            return "Error: no browser open."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        self._reset_idle()
        await self._locator(info["selector"]).select_option(label=value)
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
        # Restrict to /tmp or session state dir to prevent arbitrary file writes
        resolved = str(Path(path).resolve())
        state_dir = os.environ.get("CLAUDE_EXT_STATE_DIR", "")
        safe_prefix = state_dir.rstrip("/") + "/" if state_dir else ""
        if not (resolved.startswith("/tmp/") or (safe_prefix and resolved.startswith(safe_prefix))):
            return f"Error: screenshot path must be under /tmp/ or session state dir."
        await self._page.screenshot(path=resolved)
        return f"Screenshot saved to {resolved}"

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

    async def upload(self, ref: str, path: str) -> str:
        if not self._page:
            return "Error: no browser open."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        resolved = str(Path(path).resolve())
        state_dir = os.environ.get("CLAUDE_EXT_STATE_DIR", "")
        safe_prefix = state_dir.rstrip("/") + "/" if state_dir else ""
        if not (resolved.startswith("/tmp/") or (safe_prefix and resolved.startswith(safe_prefix))):
            return "Error: upload path must be under /tmp/ or session state dir."
        if not Path(resolved).is_file():
            return f"Error: file not found: {resolved}"
        self._reset_idle()
        await self._page.set_input_files(info["selector"], resolved)
        return f"Uploaded {resolved}"

    async def download(self, ref: str, save_dir: str = "/tmp") -> str:
        if not self._page:
            return "Error: no browser open."
        info = self._refs.get(ref)
        if not info:
            return f"Error: ref '{ref}' not found. Run snapshot first."
        # Validate save_dir: restrict to /tmp or session state dir
        resolved_dir = str(Path(save_dir).resolve())
        state_dir = os.environ.get("CLAUDE_EXT_STATE_DIR", "")
        safe_prefix = state_dir.rstrip("/") + "/" if state_dir else ""
        if not (
            resolved_dir.startswith("/tmp/")
            or resolved_dir == "/tmp"
            or (safe_prefix and resolved_dir.startswith(safe_prefix))
        ):
            return "Error: save_dir must be under /tmp/ or session state dir."
        self._reset_idle()
        async with self._page.expect_download() as download_info:
            await self._locator(info["selector"]).click()
        download = await download_info.value
        # Sanitize filename: strip path components to prevent traversal
        safe_name = Path(download.suggested_filename).name
        if not safe_name:
            safe_name = "download"
        save_path = str(Path(resolved_dir) / safe_name)
        await download.save_as(save_path)
        return f"Downloaded to {save_path}"

    async def switch_tab(self, index: int) -> str:
        if not self._context:
            return "Error: no browser open."
        pages = self._context.pages
        if index < 0 or index >= len(pages):
            return f"Error: tab index {index} out of range (0-{len(pages) - 1})."
        self._page = pages[index]
        self._frame = None
        self._refs = {}
        if hasattr(self, "_inject_evasions"):
            await self._page.route("**/*", self._inject_evasions)
        self._reset_idle()
        title = await self._page.title()
        return f"Switched to tab {index}: {title}\n  {self._page.url}"

    async def switch_frame(self, selector: str | None = None) -> str:
        if not self._page:
            return "Error: no browser open."
        if not selector:
            self._frame = None
            return "Switched to main frame."
        self._frame = self._page.frame_locator(selector)
        return f"Switched to frame: {selector}"

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
            os.environ.pop("DISPLAY", None)


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
                    "profile": {
                        "type": "string",
                        "description": "Reusable browser profile name (shares state across sessions)",
                    },
                    "proxy": {
                        "type": "string",
                        "description": "Proxy URL (e.g. socks5://host:port, http://host:port)",
                    },
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
            "name": "upload",
            "description": "Upload a file to a file input element by ref.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Element ref from snapshot (file input)",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path to upload (must be under /tmp/ or state dir)",
                    },
                },
                "required": ["ref", "path"],
            },
        },
        {
            "name": "download",
            "description": "Click a download link/button by ref and save the file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Element ref from snapshot",
                    },
                    "save_dir": {
                        "type": "string",
                        "description": "Directory to save to (default: /tmp)",
                    },
                },
                "required": ["ref"],
            },
        },
        {
            "name": "switch_tab",
            "description": "Switch to a browser tab by index.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "Tab index (0-based)",
                    },
                },
                "required": ["index"],
            },
        },
        {
            "name": "switch_frame",
            "description": "Switch to an iframe by CSS selector, or reset to main frame.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for iframe (omit to reset to main frame)",
                    },
                },
            },
        },
        {
            "name": "add_auth_domain",
            "description": "Add a domain to the auth skip list (skips evasion injection on auth sites).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain to add (e.g. 'login.example.com')",
                    },
                },
                "required": ["domain"],
            },
        },
        {
            "name": "create_profile",
            "description": "Create a fingerprint profile with deterministic random values.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Profile name (alphanumeric, hyphens, underscores)",
                    },
                    "overrides": {
                        "type": "object",
                        "description": "Override specific fingerprint values (canvas_seed, screen_width, timezone, etc.)",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_profiles",
            "description": "List all available fingerprint profiles.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "delete_profile",
            "description": "Delete a fingerprint profile.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Profile name to delete",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "close",
            "description": "Close the stealth browser and release resources.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "check_email",
            "description": "Search INBOX for emails (e.g. verification emails). Credentials from vault.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender": {
                        "type": "string",
                        "description": "Filter by sender address",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Filter by subject keyword",
                    },
                    "after": {
                        "type": "string",
                        "description": "Only emails after this ISO date (e.g. 2026-03-05)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max messages to return (default: 5)",
                    },
                },
            },
        },
        {
            "name": "read_email",
            "description": "Read full email by message ID. Extracts verification codes and links.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Message ID from check_email results",
                    },
                },
                "required": ["message_id"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        self._config = self._load_config()
        self._manager = StealthBrowserManager(self._config, bridge=self.bridge)

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
            "upload": self._handle_upload,
            "download": self._handle_download,
            "switch_tab": self._handle_switch_tab,
            "switch_frame": self._handle_switch_frame,
            "add_auth_domain": self._handle_add_auth_domain,
            "create_profile": self._handle_create_profile,
            "list_profiles": self._handle_list_profiles,
            "delete_profile": self._handle_delete_profile,
            "close": self._handle_close,
            "check_email": self._handle_check_email,
            "read_email": self._handle_read_email,
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
        profile = args.get("profile")
        proxy = args.get("proxy")
        self._run_async(self._manager.ensure_browser(url, profile=profile, proxy_override=proxy))
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

    def _handle_upload(self, args: dict) -> str:
        ref = args.get("ref", "")
        path = args.get("path", "")
        if not ref:
            return "Error: 'ref' is required."
        if not path:
            return "Error: 'path' is required."
        return self._run_async(self._manager.upload(ref, path))

    def _handle_download(self, args: dict) -> str:
        ref = args.get("ref", "")
        if not ref:
            return "Error: 'ref' is required."
        save_dir = args.get("save_dir", "/tmp")
        return self._run_async(self._manager.download(ref, save_dir))

    def _handle_switch_tab(self, args: dict) -> str:
        index = args.get("index")
        if index is None:
            return "Error: 'index' is required."
        return self._run_async(self._manager.switch_tab(int(index)))

    def _handle_switch_frame(self, args: dict) -> str:
        selector = args.get("selector")
        return self._run_async(self._manager.switch_frame(selector))

    def _handle_add_auth_domain(self, args: dict) -> str:
        domain = args.get("domain", "")
        if not domain:
            return "Error: 'domain' is required."
        return self._manager.add_auth_domain(domain)

    def _handle_create_profile(self, args: dict) -> str:
        name = args.get("name", "")
        if not name:
            return "Error: 'name' is required."
        overrides = args.get("overrides")
        return self._manager.create_profile(name, overrides)

    def _handle_list_profiles(self, args: dict) -> str:
        profiles = self._manager.list_profiles()
        if not profiles:
            return "No profiles found."
        return "\n".join(profiles)

    def _handle_delete_profile(self, args: dict) -> str:
        name = args.get("name", "")
        if not name:
            return "Error: 'name' is required."
        return self._manager.delete_profile(name)

    def _handle_close(self, args: dict) -> str:
        self._run_async(self._manager.cleanup())
        return "Browser closed."

    def _handle_check_email(self, args: dict) -> str:
        params = {
            "sender": args.get("sender"),
            "subject": args.get("subject"),
            "after": args.get("after"),
            "limit": args.get("max_results", 5),
        }
        result = self.bridge.call("stealth_email_search", params)
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}"
        messages = result.get("messages", []) if isinstance(result, dict) else []
        if not messages:
            return "No messages found."
        lines = []
        for m in messages:
            lines.append(
                f"[{m['id']}] {m['date']}\n  From: {m['sender']}\n  Subject: {m['subject']}"
            )
            if m.get("snippet"):
                lines.append(f"  Preview: {m['snippet'][:100]}")
        return "\n".join(lines)

    def _handle_read_email(self, args: dict) -> str:
        message_id = args.get("message_id", "")
        if not message_id:
            return "Error: 'message_id' is required."
        result = self.bridge.call("stealth_email_read", {"message_id": message_id})
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}"
        parts = [
            f"From: {result.get('sender', '')}",
            f"Subject: {result.get('subject', '')}",
        ]
        codes = result.get("codes", [])
        links = result.get("links", [])
        if codes:
            parts.append(f"Verification codes: {', '.join(codes)}")
        if links:
            parts.append("Verification links:")
            for link in links:
                parts.append(f"  {link}")
        parts.append(f"\n--- Body ---\n{result.get('body', '')}")
        return "\n".join(parts)


if __name__ == "__main__":
    StealthBrowserMCPServer().run()
