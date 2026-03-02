"""Unix Domain Socket RPC bridge.

Allows MCP child processes (sync, blocking) to call back into the main
asyncio process.  Line-delimited JSON protocol over a Unix socket.

BridgeServer — runs in the main asyncio event loop.
BridgeClient — runs in MCP child processes (stdlib only, sync blocking).
"""

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

# Handler: async (method, params) -> dict | None
BridgeHandler = Callable[[str, dict], Awaitable[dict | None]]


# ---------------------------------------------------------------------------
# Server (main process, async)
# ---------------------------------------------------------------------------


class BridgeServer:
    """Async Unix socket server.  Multiple handlers; first non-None wins."""

    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path)
        self._handlers: list[BridgeHandler] = []
        self._server: asyncio.AbstractServer | None = None
        self._socket_inode: int | None = None  # track our inode to avoid race

    def add_handler(self, handler: BridgeHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        # Remove stale socket file
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        # Record inode so stop() only removes our own socket file
        try:
            self._socket_inode = self.socket_path.stat().st_ino
        except OSError:
            self._socket_inode = None
        log.info("Bridge server listening on %s", self.socket_path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Only unlink if the file is still ours (same inode).
        # A newer process may have replaced it; deleting theirs causes
        # bridge failures for all their MCP children.
        try:
            current_inode = self.socket_path.stat().st_ino
            if self._socket_inode is not None and current_inode != self._socket_inode:
                log.warning(
                    "bridge.sock inode changed (%d → %d), skipping unlink (another process owns it)",
                    self._socket_inode,
                    current_inode,
                )
            else:
                self.socket_path.unlink(missing_ok=True)
        except OSError:
            # File already gone — nothing to clean up
            pass
        log.info("Bridge server stopped.")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await reader.readline()
                if not data:
                    break
                line = data.decode("utf-8").strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    response = {"error": "Invalid JSON"}
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue

                method = request.get("method", "")
                params = request.get("params", {})

                result = None
                for handler in self._handlers:
                    try:
                        result = await handler(method, params)
                    except Exception:
                        log.exception("Bridge handler error for method=%s", method)
                        result = {"error": f"Handler error for {method}"}
                    if result is not None:
                        break

                if result is None:
                    result = {"error": f"No handler for method: {method}"}

                response = {"result": result}
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass


# ---------------------------------------------------------------------------
# Client (MCP child process, sync blocking)
# ---------------------------------------------------------------------------


class BridgeClient:
    """Sync blocking client for Unix socket bridge."""

    _STALE_CHECK_INTERVAL = 1.0  # seconds between inode checks

    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)
        self._sock: socket.socket | None = None
        self._connected_inode: int | None = None
        self._last_stale_check: float = 0.0

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        try:
            self._connected_inode = os.stat(self.socket_path).st_ino
        except OSError:
            self._connected_inode = None
        self._last_stale_check = time.monotonic()

    def _is_stale(self) -> bool:
        """Check if socket file was replaced (inode mismatch)."""
        now = time.monotonic()
        if now - self._last_stale_check < self._STALE_CHECK_INTERVAL:
            return False
        self._last_stale_check = now
        try:
            current_inode = os.stat(self.socket_path).st_ino
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return self._connected_inode is not None and current_inode != self._connected_inode

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected_inode = None

    def _send_recv(self, request: str, timeout: float) -> dict:
        """Send request and read response. Raises on any I/O failure."""
        assert self._sock is not None
        self._sock.settimeout(timeout)
        self._sock.sendall(request.encode("utf-8"))
        buf = b""
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Bridge connection closed")
            buf += chunk
            if b"\n" in buf:
                break
        line = buf.split(b"\n", 1)[0]
        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid bridge response: {e}") from e
        if "error" in response:
            raise RuntimeError(f"Bridge error: {response['error']}")
        return response.get("result", {})

    def call(self, method: str, params: dict | None = None, timeout: float = 300) -> dict:
        """Send an RPC call and block for the response.

        Raises TimeoutError, ConnectionError, or RuntimeError on failure.
        Auto-reconnects on stale connection (inode mismatch) and retries once
        on broken pipe / connection reset.
        """
        # Proactive staleness check: detect socket replaced after restart
        if self._sock is not None and self._is_stale():
            self._close_socket()

        if self._sock is None:
            try:
                self._connect()
            except OSError as e:
                raise ConnectionError(f"Cannot connect to bridge: {e}") from e

        request = json.dumps({"method": method, "params": params or {}}) + "\n"

        try:
            return self._send_recv(request, timeout)
        except TimeoutError:
            self._close_socket()
            raise TimeoutError(f"Bridge call timed out after {timeout}s") from None
        except (OSError, ConnectionError):
            # Transparent retry: reconnect and resend once
            self._close_socket()
            try:
                self._connect()
                return self._send_recv(request, timeout)
            except TimeoutError:
                self._close_socket()
                raise TimeoutError(f"Bridge call timed out after {timeout}s") from None
            except OSError as e:
                self._close_socket()
                raise ConnectionError(f"Bridge communication error: {e}") from e

    def close(self) -> None:
        self._close_socket()
