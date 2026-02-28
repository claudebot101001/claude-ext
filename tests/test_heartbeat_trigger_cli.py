"""Tests for heartbeat trigger_cli.py standalone CLI script."""

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

from extensions.heartbeat.trigger_cli import main


def _fake_bridge_server(socket_path: str, response: dict, received: list):
    """Run a minimal Unix socket server that records the request and sends response."""
    import socket as sock_mod

    server = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    server.settimeout(5)

    try:
        conn, _ = server.accept()
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        received.append(json.loads(buf.decode().strip()))
        conn.sendall((json.dumps(response) + "\n").encode())
        conn.close()
    finally:
        server.close()


class TestTriggerCLI:
    def test_trigger_immediate_success(self, tmp_path):
        sock_path = str(tmp_path / "bridge.sock")
        received = []
        response = {"result": {"ok": True, "urgency": "immediate"}}

        t = threading.Thread(target=_fake_bridge_server, args=(sock_path, response, received))
        t.start()

        # Small delay to let server bind
        import time; time.sleep(0.1)

        exit_code = main(["--socket", sock_path, "transfer_done"])
        t.join(timeout=5)

        assert exit_code == 0
        assert len(received) == 1
        req = received[0]
        assert req["method"] == "heartbeat_trigger"
        assert req["params"]["event_type"] == "transfer_done"
        assert req["params"]["urgency"] == "immediate"
        assert req["params"]["source"] == "external"
        assert req["params"]["payload"] is None

    def test_trigger_with_payload(self, tmp_path):
        sock_path = str(tmp_path / "bridge.sock")
        received = []
        response = {"result": {"ok": True, "urgency": "immediate"}}

        t = threading.Thread(target=_fake_bridge_server, args=(sock_path, response, received))
        t.start()
        import time; time.sleep(0.1)

        payload = {"asset": "BTC", "price": 95000}
        exit_code = main([
            "--socket", sock_path,
            "price_alert",
            "--payload", json.dumps(payload),
        ])
        t.join(timeout=5)

        assert exit_code == 0
        req = received[0]
        assert req["params"]["payload"] == payload

    def test_trigger_normal_urgency(self, tmp_path):
        sock_path = str(tmp_path / "bridge.sock")
        received = []
        response = {"result": {"ok": True, "urgency": "normal"}}

        t = threading.Thread(target=_fake_bridge_server, args=(sock_path, response, received))
        t.start()
        import time; time.sleep(0.1)

        exit_code = main(["--socket", sock_path, "data_ready", "--urgency", "normal"])
        t.join(timeout=5)

        assert exit_code == 0
        req = received[0]
        assert req["params"]["urgency"] == "normal"

    def test_trigger_custom_source(self, tmp_path):
        sock_path = str(tmp_path / "bridge.sock")
        received = []
        response = {"result": {"ok": True, "urgency": "immediate"}}

        t = threading.Thread(target=_fake_bridge_server, args=(sock_path, response, received))
        t.start()
        import time; time.sleep(0.1)

        exit_code = main([
            "--socket", sock_path,
            "build_done",
            "--source", "ci-pipeline",
        ])
        t.join(timeout=5)

        assert exit_code == 0
        req = received[0]
        assert req["params"]["source"] == "ci-pipeline"

    def test_trigger_socket_not_found(self, tmp_path):
        sock_path = str(tmp_path / "nonexistent.sock")
        exit_code = main(["--socket", sock_path, "test_event"])
        assert exit_code == 1

    def test_trigger_error_response(self, tmp_path):
        sock_path = str(tmp_path / "bridge.sock")
        received = []
        response = {"result": {"error": "event_type is required"}}

        t = threading.Thread(target=_fake_bridge_server, args=(sock_path, response, received))
        t.start()
        import time; time.sleep(0.1)

        exit_code = main(["--socket", sock_path, "test"])
        t.join(timeout=5)

        assert exit_code == 1

    def test_trigger_invalid_payload_json(self, tmp_path):
        """Invalid JSON in --payload should fail with exit code 1."""
        sock_path = str(tmp_path / "bridge.sock")
        exit_code = main(["--socket", sock_path, "test", "--payload", "not-json"])
        assert exit_code == 1
