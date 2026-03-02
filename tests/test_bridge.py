"""Tests for BridgeClient proactive reconnect and retry logic."""

import json
import os
import socket
import threading
import time
from unittest.mock import patch

import pytest

from core.bridge import BridgeClient


def _echo_server(sock_path: str, ready: threading.Event, stop: threading.Event):
    """Minimal echo server: returns {"result": params} for any call."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(0.5)
    ready.set()
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            continue
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if data:
                req = json.loads(data.decode())
                resp = json.dumps({"result": req.get("params", {})}) + "\n"
                conn.sendall(resp.encode())
        finally:
            conn.close()
    srv.close()


@pytest.fixture
def bridge_env(tmp_path):
    """Start an echo server, yield (socket_path, stop_event)."""
    sock_path = str(tmp_path / "bridge.sock")
    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(target=_echo_server, args=(sock_path, ready, stop), daemon=True)
    t.start()
    ready.wait(timeout=5)
    yield sock_path, stop, tmp_path
    stop.set()
    t.join(timeout=5)


class TestBridgeClientBasic:
    def test_call_returns_result(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        result = client.call("test", {"key": "value"}, timeout=5)
        assert result == {"key": "value"}
        client.close()

    def test_inode_tracked_on_connect(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)
        assert client._connected_inode is not None
        assert client._connected_inode == os.stat(sock_path).st_ino
        client.close()


class TestStalenessDetection:
    def test_is_stale_returns_false_for_same_inode(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)
        # Force stale check (bypass interval throttle)
        client._last_stale_check = 0.0
        assert not client._is_stale()
        client.close()

    def test_is_stale_returns_true_when_socket_deleted(self, bridge_env):
        sock_path, stop, _ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)
        stop.set()
        time.sleep(0.1)
        os.unlink(sock_path)
        client._last_stale_check = 0.0
        assert client._is_stale()

    def test_is_stale_returns_true_when_inode_changes(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)

        # Simulate stale state: connected inode no longer matches socket on disk
        client._connected_inode = client._connected_inode + 99999
        client._last_stale_check = 0.0
        assert client._is_stale()
        client.close()

    def test_stale_check_throttled(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)
        # Just called — should be throttled (returns False without checking)
        with patch("os.stat") as mock_stat:
            assert not client._is_stale()
            mock_stat.assert_not_called()
        client.close()


class TestProactiveReconnect:
    def test_proactive_stale_reconnect(self, bridge_env):
        sock_path, stop, _tmp_path = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)

        # Restart server (new inode)
        stop.set()
        time.sleep(0.1)
        os.unlink(sock_path)

        ready2 = threading.Event()
        stop2 = threading.Event()
        t2 = threading.Thread(target=_echo_server, args=(sock_path, ready2, stop2), daemon=True)
        t2.start()
        ready2.wait(timeout=5)

        # Force stale check to trigger proactively (bypass throttle)
        client._last_stale_check = 0.0
        result = client.call("proactive", {"ok": True}, timeout=5)
        assert result == {"ok": True}
        # Verify inode was updated to the new socket
        assert client._connected_inode == os.stat(sock_path).st_ino

        stop2.set()
        t2.join(timeout=5)
        client.close()


class TestTransparentRetry:
    def test_retry_on_broken_pipe(self, bridge_env):
        sock_path, stop, _tmp_path = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)

        # Kill old server, start new one (simulates main process restart)
        stop.set()
        time.sleep(0.1)
        os.unlink(sock_path)

        ready2 = threading.Event()
        stop2 = threading.Event()
        t2 = threading.Thread(target=_echo_server, args=(sock_path, ready2, stop2), daemon=True)
        t2.start()
        ready2.wait(timeout=5)

        # Client still holds old fd — call should retry transparently
        result = client.call("retry_test", {"ok": True}, timeout=5)
        assert result == {"ok": True}

        stop2.set()
        t2.join(timeout=5)
        client.close()

    def test_raises_connection_error_when_server_down(self, bridge_env):
        sock_path, stop, _ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)

        # Kill server, don't restart
        stop.set()
        time.sleep(0.1)
        os.unlink(sock_path)

        with pytest.raises(ConnectionError):
            client.call("fail", timeout=2)


class TestCloseSocket:
    def test_close_clears_state(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)
        assert client._sock is not None
        assert client._connected_inode is not None
        client.close()
        assert client._sock is None
        assert client._connected_inode is None

    def test_double_close_safe(self, bridge_env):
        sock_path, *_ = bridge_env
        client = BridgeClient(sock_path)
        client.call("ping", timeout=5)
        client.close()
        client.close()  # Should not raise
