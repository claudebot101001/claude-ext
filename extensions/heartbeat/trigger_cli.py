#!/usr/bin/env python3
"""Standalone heartbeat trigger for external processes.

Pure stdlib — no project imports, no pip dependencies.  Can be called by
any process (cron, nohup background job, monitoring script) to wake the
heartbeat scheduler via the bridge Unix socket.

Usage:
    python3 trigger_cli.py --socket /path/bridge.sock EVENT_TYPE
    python3 trigger_cli.py --socket /path/bridge.sock EVENT_TYPE --urgency normal
    python3 trigger_cli.py --socket /path/bridge.sock EVENT_TYPE --payload '{"key": "val"}'

Chain with long-running commands:
    rsync -av /src/ /dst/ && python3 trigger_cli.py --socket /path/bridge.sock transfer_done
    nohup bash -c 'make build && python3 trigger_cli.py --socket ...' &
"""

import argparse
import json
import socket
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Trigger heartbeat via bridge socket")
    p.add_argument("event_type", help="Event category (e.g. 'transfer_done', 'price_alert')")
    p.add_argument("--socket", required=True, help="Path to bridge.sock")
    p.add_argument("--urgency", default="immediate", choices=["immediate", "normal"])
    p.add_argument("--payload", default=None, help="JSON string with event data")
    p.add_argument("--source", default="external", help="Event source identifier")
    args = p.parse_args(argv)

    payload = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"Invalid --payload JSON: {e}", file=sys.stderr)
            return 1

    request = {
        "method": "heartbeat_trigger",
        "params": {
            "source": args.source,
            "event_type": args.event_type,
            "urgency": args.urgency,
            "payload": payload,
        },
    }

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(args.socket)
        sock.sendall((json.dumps(request) + "\n").encode())

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk

        resp = json.loads(buf.decode().strip())
        result = resp.get("result", {})
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0
    except Exception as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
