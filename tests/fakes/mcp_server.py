"""A tiny MCP server over stdio, used to exercise StdioMcpClient for real.

Run as a script. Supports initialize, tools/list, and tools/call with a
handful of tools whose behaviour is chosen by name so tests can drive
timeouts, crashes, tool errors, and JSON-RPC errors.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import payloads  # noqa: E402

TOOLS = [
    {"name": "gibwork_wallet_status", "inputSchema": {"type": "object"}},
    {"name": "gibwork_task_list", "inputSchema": {"type": "object"}},
    {"name": "fake_echo", "inputSchema": {"type": "object"}},
    {"name": "fake_text_json", "inputSchema": {"type": "object"}},
    {"name": "fake_text_plain", "inputSchema": {"type": "object"}},
    {"name": "fake_error", "inputSchema": {"type": "object"}},
    {"name": "fake_hang", "inputSchema": {"type": "object"}},
    {"name": "fake_crash", "inputSchema": {"type": "object"}},
    {"name": "fake_env", "inputSchema": {"type": "object"}},
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def structured(req_id: int, body: dict, *, is_error: bool = False) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(body)}],
                "structuredContent": body,
                "isError": is_error,
            },
        }
    )


def handle_call(req_id: int, name: str, args: dict) -> None:
    if name == "gibwork_wallet_status":
        structured(req_id, payloads.WALLET_STATUS)
    elif name == "gibwork_task_list":
        structured(req_id, payloads.TASK_LIST)
    elif name == "fake_echo":
        structured(req_id, {"echo": args})
    elif name == "fake_text_json":
        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"from": "text"})}]},
            }
        )
    elif name == "fake_text_plain":
        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "not json"}]},
            }
        )
    elif name == "fake_error":
        structured(req_id, args.get("payload") or payloads.ERROR_CONFIRMATION_UNKNOWN, is_error=True)
    elif name == "fake_hang":
        time.sleep(float(args.get("seconds", 5)))
        structured(req_id, {"late": True})
    elif name == "fake_crash":
        sys.stderr.write("fake server: crashing on purpose\n")
        sys.stderr.flush()
        os._exit(3)
    elif name == "fake_env":
        structured(
            req_id,
            {
                "has_private_key": "GIBWORK_PRIVATE_KEY" in os.environ,
                "has_keypair_path": "GIBWORK_KEYPAIR_PATH" in os.environ,
                "argv": sys.argv[1:],
            },
        )
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"unknown tool {name}"},
            }
        )


def main() -> None:
    # Emit some stderr noise like a real server would.
    sys.stderr.write("fake mcp server starting\n")
    sys.stderr.flush()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        req_id = message.get("id")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-gibwork", "version": "0.0.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            # Send an unsolicited notification to make sure clients ignore it.
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            handle_call(req_id, params.get("name", ""), params.get("arguments") or {})
        elif req_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
