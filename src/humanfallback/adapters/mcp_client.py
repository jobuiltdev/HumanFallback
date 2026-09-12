"""Minimal synchronous MCP client over stdio.

Speaks newline-delimited JSON-RPC 2.0 to a child process: one `initialize`
handshake, then `tools/call` requests. Deliberately small so it can be
replaced by a fake in tests and so every byte sent to the server is
visible in one file.

Transport failures carry a `dispatched` flag: True means the request
bytes were handed to the child before the failure, so a money-moving call
may or may not have taken effect.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from collections.abc import Sequence
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "humanfallback", "version": "0.2.0"}

# Environment variables that must never reach the child process from here.
# Credentials are resolved by the Gibwork CLI from its own profile config.
SCRUBBED_ENV = ("GIBWORK_PRIVATE_KEY",)

_STDERR_KEEP = 40


class McpTransportError(Exception):
    def __init__(self, message: str, *, dispatched: bool) -> None:
        super().__init__(message)
        self.dispatched = dispatched


class McpProtocolError(Exception):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class McpToolError(Exception):
    """The tool ran but reported `isError`. `payload` is its decoded body."""

    def __init__(self, payload: Any) -> None:
        super().__init__(str(payload)[:500])
        self.payload = payload


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in SCRUBBED_ENV:
        env.pop(key, None)
    return env


class StdioMcpClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = list(command)
        self.timeout = timeout
        self._env = scrubbed_env(env)
        self._cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_STDERR_KEEP)
        self._next_id = 1
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
            )
        except OSError as exc:
            raise McpTransportError(f"could not start {self.command[0]}: {exc}", dispatched=False)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            money=False,
        )
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    def __enter__(self) -> StdioMcpClient:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    # -- MCP ----------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {}, money=False)
        return list(result.get("tools", [])) if isinstance(result, dict) else []

    def call_tool(self, name: str, arguments: dict[str, Any], *, money: bool = False) -> Any:
        """Call one tool and return its decoded result body.

        Raises McpToolError when the tool reports isError, McpProtocolError on a
        JSON-RPC error, and McpTransportError when the process or pipe fails.
        """
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}, money=money
        )
        if not isinstance(result, dict):
            raise McpProtocolError(-32000, f"unexpected tools/call result: {result!r}")
        body = _decode_body(result)
        if result.get("isError"):
            raise McpToolError(body)
        return body

    # -- JSON-RPC plumbing --------------------------------------------------

    def _request(self, method: str, params: dict[str, Any], *, money: bool) -> Any:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            # From here on the request may have reached the server.
            try:
                return self._await(req_id)
            except McpTransportError as exc:
                raise McpTransportError(str(exc), dispatched=True) from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpTransportError("client is not started", dispatched=False)
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpTransportError(
                f"write to server failed: {exc}{self._stderr_hint()}", dispatched=True
            ) from exc

    def _await(self, req_id: int) -> Any:
        while True:
            try:
                line = self._lines.get(timeout=self.timeout)
            except queue.Empty:
                raise McpTransportError(
                    f"timed out after {self.timeout}s waiting for response {req_id}"
                    f"{self._stderr_hint()}",
                    dispatched=True,
                )
            if line is None:
                raise McpTransportError(
                    f"server exited before responding{self._stderr_hint()}", dispatched=True
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # not JSON-RPC; ignore stray output
            if not isinstance(message, dict):
                continue
            if message.get("id") == req_id and ("result" in message or "error" in message):
                if "error" in message:
                    err = message["error"] or {}
                    raise McpProtocolError(
                        int(err.get("code", -32000)), str(err.get("message", "error")), err.get("data")
                    )
                return message.get("result")
            if message.get("method") == "ping" and "id" in message:
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            # Notifications and unrelated responses are ignored.

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _stderr_hint(self) -> str:
        if not self._stderr:
            return ""
        return " | stderr: " + " / ".join(list(self._stderr)[-3:])


def _decode_body(result: dict[str, Any]) -> Any:
    """Prefer structuredContent; otherwise decode the first text block as JSON."""
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return {"text": text}
    return {}
