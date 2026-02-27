"""Unix Domain Socket RPC bridge.

Allows MCP child processes (sync, blocking) to call back into the main
asyncio process.  Line-delimited JSON protocol over a Unix socket.

BridgeServer — runs in the main asyncio event loop.
BridgeClient — runs in MCP child processes (stdlib only, sync blocking).
"""

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Awaitable, Callable

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

    def add_handler(self, handler: BridgeHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        # Remove stale socket file
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path),
        )
        log.info("Bridge server listening on %s", self.socket_path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)
        log.info("Bridge server stopped.")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
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

    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)
        self._sock: socket.socket | None = None

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self._sock = sock

    def call(self, method: str, params: dict | None = None, timeout: float = 300) -> dict:
        """Send an RPC call and block for the response.

        Raises TimeoutError, ConnectionError, or RuntimeError on failure.
        Auto-reconnects on stale connection.
        """
        if self._sock is None:
            try:
                self._connect()
            except OSError as e:
                raise ConnectionError(f"Cannot connect to bridge: {e}") from e

        request = json.dumps({"method": method, "params": params or {}}) + "\n"

        try:
            self._sock.settimeout(timeout)
            self._sock.sendall(request.encode("utf-8"))

            # Read response line
            buf = b""
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionError("Bridge connection closed")
                buf += chunk
                if b"\n" in buf:
                    break
        except socket.timeout:
            self._sock = None
            raise TimeoutError(f"Bridge call timed out after {timeout}s")
        except OSError as e:
            self._sock = None
            raise ConnectionError(f"Bridge communication error: {e}") from e

        line = buf.split(b"\n", 1)[0]
        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid bridge response: {e}") from e

        if "error" in response:
            raise RuntimeError(f"Bridge error: {response['error']}")

        return response.get("result", {})

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
