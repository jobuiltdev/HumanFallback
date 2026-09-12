"""Exercise StdioMcpClient against a real child process (the fake server)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from humanfallback.adapters.mcp_client import (
    McpProtocolError,
    McpToolError,
    McpTransportError,
    StdioMcpClient,
    scrubbed_env,
)

SERVER = Path(__file__).parent / "fakes" / "mcp_server.py"


def _client(timeout: float = 10.0, *extra: str) -> StdioMcpClient:
    return StdioMcpClient([sys.executable, str(SERVER), *extra], timeout=timeout)


def test_handshake_and_tools_list() -> None:
    with _client() as client:
        assert client.server_info["name"] == "fake-gibwork"
        names = {t["name"] for t in client.list_tools()}
        assert "gibwork_wallet_status" in names


def test_call_returns_structured_content() -> None:
    with _client() as client:
        body = client.call_tool("gibwork_wallet_status", {})
        assert body["environment"] == "stage"
        assert body["walletAddress"].startswith("G27")


def test_call_falls_back_to_text_json() -> None:
    with _client() as client:
        assert client.call_tool("fake_text_json", {}) == {"from": "text"}


def test_call_wraps_plain_text() -> None:
    with _client() as client:
        assert client.call_tool("fake_text_plain", {}) == {"text": "not json"}


def test_arguments_round_trip() -> None:
    with _client() as client:
        body = client.call_tool("fake_echo", {"a": 1, "b": ["x"]})
        assert body == {"echo": {"a": 1, "b": ["x"]}}


def test_tool_error_carries_payload() -> None:
    with _client() as client:
        with pytest.raises(McpToolError) as exc:
            client.call_tool("fake_error", {})
        assert exc.value.payload["error"]["code"] == "PENDING_INTENT_ERROR"


def test_unknown_tool_is_protocol_error() -> None:
    with _client() as client:
        with pytest.raises(McpProtocolError) as exc:
            client.call_tool("does_not_exist", {})
        assert exc.value.code == -32602


def test_timeout_is_dispatched_transport_error() -> None:
    with _client(timeout=0.5) as client:
        with pytest.raises(McpTransportError) as exc:
            client.call_tool("fake_hang", {"seconds": 3}, money=True)
        assert exc.value.dispatched is True
        assert "timed out" in str(exc.value)


def test_crash_is_dispatched_transport_error_with_stderr() -> None:
    with _client() as client:
        with pytest.raises(McpTransportError) as exc:
            client.call_tool("fake_crash", {})
        assert exc.value.dispatched is True
        assert "exited" in str(exc.value)
        assert any("crashing on purpose" in line for line in client.stderr_tail)


def test_missing_executable_is_not_dispatched() -> None:
    client = StdioMcpClient([str(Path("/nonexistent/binary"))], timeout=1)
    with pytest.raises(McpTransportError) as exc:
        client.start()
    assert exc.value.dispatched is False


def test_private_key_env_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIBWORK_PRIVATE_KEY", "should-never-reach-child")
    monkeypatch.setenv("GIBWORK_KEYPAIR_PATH", "C:/some/path/id.json")
    env = scrubbed_env()
    assert "GIBWORK_PRIVATE_KEY" not in env
    assert env["GIBWORK_KEYPAIR_PATH"] == "C:/some/path/id.json"
    with _client() as client:
        body = client.call_tool("fake_env", {})
        assert body["has_private_key"] is False
        assert body["has_keypair_path"] is True


def test_extra_args_reach_server() -> None:
    with _client(10.0, "--read-only") as client:
        assert client.call_tool("fake_env", {})["argv"] == ["--read-only"]


def test_close_is_idempotent() -> None:
    client = _client()
    client.start()
    client.close()
    client.close()
