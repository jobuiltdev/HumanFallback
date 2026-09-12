"""In-process stand-in for StdioMcpClient used by adapter and CLI tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from humanfallback.adapters.mcp_client import McpToolError

Handler = Callable[[dict[str, Any]], Any]


class FakeSession:
    """Dispatches tool calls to handlers and records everything."""

    def __init__(self, handlers: dict[str, Handler] | None = None) -> None:
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.started = False
        self.closed = False
        self.command: list[str] | None = None

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def call_tool(self, name: str, arguments: dict[str, Any], *, money: bool = False) -> Any:
        self.calls.append((name, dict(arguments), money))
        handler = self.handlers.get(name)
        if handler is None:
            raise McpToolError({"error": {"code": "USAGE_ERROR", "message": f"no handler for {name}"}})
        return handler(arguments)

    def count(self, name: str) -> int:
        return sum(1 for n, _, _ in self.calls if n == name)


def tool_error(payload: dict[str, Any]) -> Handler:
    def handler(_: dict[str, Any]) -> Any:
        raise McpToolError(payload)

    return handler


def returns(payload: Any) -> Handler:
    return lambda _: payload


def raises(exc: Exception) -> Handler:
    def handler(_: dict[str, Any]) -> Any:
        raise exc

    return handler


class FakeSessionFactory:
    """Callable matching GibworkMcpAdapter's session_factory; records the command."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> FakeSession:
        self.commands.append(list(command))
        self.session.command = list(command)
        return self.session
