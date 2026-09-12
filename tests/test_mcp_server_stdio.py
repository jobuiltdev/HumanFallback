"""End-to-end: spawn `hf mcp serve --adapter mock` and talk MCP over stdio."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from humanfallback.adapters.mcp_client import StdioMcpClient
from humanfallback.mcp_server import PREFIX, claude_code_snippet

REQ = "Go to the hardware store and take a photo of the shelf."


@pytest.fixture
def server(tmp_path: Path) -> StdioMcpClient:
    env = {**os.environ, "HF_HOME": str(tmp_path), "HF_ADAPTER": "mock"}
    client = StdioMcpClient(
        [sys.executable, "-m", "humanfallback.cli", "mcp", "serve"], timeout=60, env=env
    )
    with client:
        yield client


def test_handshake_lists_only_safe_tools(server: StdioMcpClient) -> None:
    assert server.server_info.get("name") == "humanfallback"
    names = {t["name"] for t in server.list_tools()}
    assert PREFIX + "delegate_prepare" in names
    assert not any("submit" in n or "refund" in n or "resolve" in n for n in names)


def test_full_flow_over_stdio(server: StdioMcpClient) -> None:
    status = server.call_tool(PREFIX + "status", {})
    assert status["adapter"] == "mock" and status["money_moving_tools_exposed"] is False

    created = server.call_tool(PREFIX + "contract_create", {"text": REQ, "reward": "2.00"})
    cid = created["contract"]["id"]
    assert created["contract"]["status"] == "ready"

    prepared = server.call_tool(PREFIX + "delegate_prepare", {"contract_id": cid})
    assert prepared["funds_moved"] is False
    assert prepared["confirmation"]["usable_for_submit"] is False
    assert prepared["quote"]["total_debit"] == "2.00"

    got = server.call_tool(PREFIX + "contract_get", {"contract_id": cid})
    assert got["contract"]["status"] == "ready"
    assert got["contract"]["attempts"][-1]["outcome"] == "prepared"

    wallet = server.call_tool(PREFIX + "wallet", {})
    assert wallet["adapter"] == "mock"


def test_errors_are_structured_over_stdio(server: StdioMcpClient) -> None:
    from humanfallback.adapters.mcp_client import McpToolError

    with pytest.raises(McpToolError) as exc:
        server.call_tool(PREFIX + "contract_get", {"contract_id": "missing"})
    assert exc.value.payload["error"]["code"] == "NOT_FOUND"


def test_adapter_flag_is_refused_when_unknown(tmp_path: Path) -> None:
    env = {**os.environ, "HF_HOME": str(tmp_path)}
    client = StdioMcpClient(
        [sys.executable, "-m", "humanfallback.cli", "mcp", "serve", "--adapter", "nope"], timeout=30, env=env
    )
    from humanfallback.adapters.mcp_client import McpTransportError

    with pytest.raises(McpTransportError):
        client.start()
    assert any("unknown adapter" in line for line in client.stderr_tail)
    client.close()


class TestSnippet:
    def test_mock_snippet(self) -> None:
        text = claude_code_snippet("mock", project_dir="/proj")
        assert "claude mcp add humanfallback --" in text
        assert "run hf mcp serve" in text
        assert "HF_ADAPTER" not in text
        block = text[text.index("{") :]
        cfg = json.loads(block)
        assert cfg["mcpServers"]["humanfallback"]["args"] == ["--directory", "/proj", "run", "hf", "mcp", "serve"]

    def test_gibwork_snippet(self) -> None:
        text = claude_code_snippet("gibwork", project_dir="/proj")
        assert "humanfallback-gibwork" in text
        assert "-e HF_ADAPTER=gibwork" in text
        assert "keypair-path" in text and "never via HumanFallback" in text
        block = text[text.index("{") : text.index("}\n}") + 3]
        cfg = json.loads(block)
        assert cfg["mcpServers"]["humanfallback-gibwork"]["env"] == {"HF_ADAPTER": "gibwork"}
