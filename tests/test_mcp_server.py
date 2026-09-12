"""HumanFallback MCP server driven through the SDK's in-memory client.

Covers tool schemas, validation, adapter error envelopes, and the safety
boundary: no money-moving tools, ephemeral confirmations, uncertain-submit
locking, pinned adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from mcp.types import CallToolResult

from fakes import payloads as P
from fakes.session import FakeSession, FakeSessionFactory, raises, returns, tool_error
from humanfallback import __version__
from humanfallback.adapters import GibworkMcpAdapter, MockGibworkAdapter
from humanfallback.adapters.mcp_client import McpTransportError
from humanfallback.mcp_server import CONFIRMATION_NOTE, PREFIX, build_server
from humanfallback.service import Service
from humanfallback.store import ContractStore

REQ = "Go to the hardware store and take a photo of the shelf."
EXPECTED_TOOLS = {
    "classify",
    "contract_create",
    "contract_get",
    "contract_list",
    "contract_refresh",
    "contract_reconcile",
    "delegate_prepare",
    "submission_list",
    "submission_get",
    "review_submission",
    "review_all",
    "review_rank",
    "wallet",
    "status",
}
FORBIDDEN_FRAGMENTS = ("submit_", "_submit", "refund", "resolve", "approve", "reject", "pay")


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


class Harness:
    """Synchronous facade over the in-memory MCP client."""

    def __init__(self, service: Service) -> None:
        self.service = service
        self.server = build_server(service)

    def call(self, tool: str, **args: Any) -> CallToolResult:
        async def go() -> CallToolResult:
            async with Client(self.server) as client:
                return await client.call_tool(PREFIX + tool, args)

        return run(go())

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self.call(tool, **args)
        assert not result.is_error, result.content
        assert result.structured_content is not None
        return result.structured_content

    def err(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self.call(tool, **args)
        assert result.is_error, result.structured_content
        assert result.structured_content is not None, [c.text for c in result.content]  # type: ignore[union-attr]
        return result.structured_content["error"]

    def tools(self) -> list[Any]:
        async def go() -> list[Any]:
            async with Client(self.server) as client:
                return (await client.list_tools()).tools

        return run(go())


@pytest.fixture
def mock_state(tmp_path: Path) -> Path:
    return tmp_path / "mock.json"


@pytest.fixture
def harness(tmp_path: Path, mock_state: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    service = Service(
        store_factory=lambda: ContractStore(tmp_path / "hf.db"),
        adapter_factory=lambda writes: MockGibworkAdapter(state_path=mock_state),
        adapter_name="mock",
    )
    return Harness(service)


def _gibwork_harness(tmp_path: Path, session: FakeSession) -> tuple[Harness, list[bool]]:
    writes_seen: list[bool] = []

    def factory(writes: bool) -> GibworkMcpAdapter:
        writes_seen.append(writes)
        return GibworkMcpAdapter(
            command=["node", "bin.js"], writes=writes, session_factory=FakeSessionFactory(session)
        )

    service = Service(
        store_factory=lambda: ContractStore(tmp_path / "hf.db"),
        adapter_factory=factory,
        adapter_name="gibwork",
    )
    return Harness(service), writes_seen


def _happy() -> FakeSession:
    return FakeSession(
        {
            "gibwork_wallet_status": returns(P.WALLET_STATUS),
            "gibwork_task_list": returns(P.TASK_LIST_EMPTY),
            "gibwork_task_create_prepare": returns(P.PREPARE_RESULT),
            "gibwork_task_create_submit": returns(P.SUBMIT_RESULT),
            "gibwork_task_refund_prepare": returns(P.REFUND_PREPARE_RESULT),
            "gibwork_task_refund_submit": returns({"signature": "rs"}),
            "gibwork_submission_list": returns(P.SUBMISSION_LIST),
            "gibwork_submission_get": returns(P.SUBMISSION_ITEM),
        }
    )


# -- schemas --------------------------------------------------------------------


class TestSchemas:
    def test_exact_tool_set(self, harness: Harness) -> None:
        names = {t.name for t in harness.tools()}
        assert names == {PREFIX + n for n in EXPECTED_TOOLS}

    def test_no_money_moving_tool_names(self, harness: Harness) -> None:
        for tool in harness.tools():
            for fragment in FORBIDDEN_FRAGMENTS:
                assert fragment not in tool.name, tool.name

    def test_every_tool_has_description_and_schemas(self, harness: Harness) -> None:
        for tool in harness.tools():
            assert tool.description, tool.name
            assert tool.input_schema["type"] == "object"
            assert tool.output_schema is not None, tool.name
            refs = {x.get("$ref", "") for x in tool.output_schema.get("anyOf", [])}
            assert any(r.endswith("/ErrorEnvelope") for r in refs), tool.name

    def test_required_inputs(self, harness: Harness) -> None:
        required = {t.name: t.input_schema.get("required", []) for t in harness.tools()}
        assert required[PREFIX + "classify"] == ["text"]
        assert required[PREFIX + "contract_create"] == ["text"]
        assert required[PREFIX + "contract_get"] == ["contract_id"]
        assert required[PREFIX + "delegate_prepare"] == ["contract_id"]
        assert required[PREFIX + "submission_get"] == ["contract_id", "submission_id"]
        assert required[PREFIX + "wallet"] == []
        assert required[PREFIX + "status"] == []

    def test_no_tool_accepts_adapter_or_confirmation(self, harness: Harness) -> None:
        for tool in harness.tools():
            props = set(tool.input_schema.get("properties", {}))
            assert "adapter" not in props, tool.name
            assert not any("confirmation" in p for p in props), tool.name

    def test_annotations(self, harness: Harness) -> None:
        ann = {t.name: t.annotations for t in harness.tools()}
        read_only = {"classify", "contract_get", "contract_list", "submission_list", "submission_get",
                     "review_submission", "review_all", "review_rank", "wallet", "status"}
        for name, a in ann.items():
            assert a is not None
            assert a.destructive_hint is False
            assert a.read_only_hint is (name.removeprefix(PREFIX) in read_only), name

    def test_server_instructions_forbid_confirmation_reuse(self, harness: Harness) -> None:
        text = harness.server.instructions or ""
        assert "cannot move money" in text
        assert "hf delegate" in text
        assert "reuse a confirmation" in text


# -- validation and local tools ------------------------------------------------


class TestLocalTools:
    def test_status_never_spawns_backend(self, tmp_path: Path) -> None:
        session = _happy()
        h, writes = _gibwork_harness(tmp_path, session)
        body = h.ok("status")
        assert body["version"] == __version__
        assert body["adapter"] == "gibwork"
        assert body["adapter_pinned"] is True
        assert body["money_moving_tools_exposed"] is False
        assert session.calls == [] and writes == []

    def test_classify(self, harness: Harness) -> None:
        body = harness.ok("classify", text=REQ)
        assert body["classification"]["human_required"] is True
        assert "contract_create" in body["recommended_action"]
        body = harness.ok("classify", text="Summarize this document.")
        assert body["classification"]["human_required"] is False

    def test_create_get_list(self, harness: Harness) -> None:
        created = harness.ok("contract_create", text=REQ, reward="2.50", tags=["errand"])
        contract = created["contract"]
        assert contract["status"] == "ready"
        assert created["locked"] is False
        assert created["allowed_actions"] == [PREFIX + "delegate_prepare"]
        assert "hf delegate" in created["human_action_required"]

        got = harness.ok("contract_get", contract_id=contract["id"])
        assert got["contract"]["id"] == contract["id"]

        listed = harness.ok("contract_list")
        assert listed["total"] == 1
        assert listed["contracts"][0]["reward_amount"] == "2.50"
        assert harness.ok("contract_list", status="draft")["total"] == 0

    def test_agent_capable_error_and_force(self, harness: Harness) -> None:
        err = harness.err("contract_create", text="Summarize this document.")
        assert err["code"] == "AGENT_CAPABLE"
        assert "classification" in err["details"]
        body = harness.ok("contract_create", text="Summarize this document.", force=True)
        assert body["contract"]["status"] == "draft"
        assert body["allowed_actions"] == []

    def test_validation_errors(self, harness: Harness) -> None:
        assert harness.err("contract_create", text=REQ, reward="5")["code"] == "VALIDATION"
        assert harness.err("contract_create", text=REQ, tags=["a", "b", "c", "d"])["code"] == "VALIDATION"
        assert harness.err("contract_get", contract_id="missing")["code"] == "NOT_FOUND"
        # schema-level rejection (wrong type) is reported by the SDK as a tool error
        result = harness.call("contract_list", limit="many")
        assert result.is_error

    def test_bad_enum_filter_rejected(self, harness: Harness) -> None:
        result = harness.call("contract_list", status="bogus")
        assert result.is_error


# -- delegation preparation and safety -------------------------------------------


class TestDelegatePrepare:
    def test_prepare_is_ephemeral_and_moves_nothing(self, harness: Harness, mock_state: Path) -> None:
        cid = harness.ok("contract_create", text=REQ, reward="3.00")["contract"]["id"]
        body = harness.ok("delegate_prepare", contract_id=cid)
        assert body["status"] == "prepared"
        assert body["funds_moved"] is False
        assert body["quote"]["total_debit"] == "3.00"
        assert body["contract_status"] == "ready"
        conf = body["confirmation"]
        assert conf["ephemeral"] is True
        assert conf["usable_for_submit"] is False
        assert conf["note"] == CONFIRMATION_NOTE
        assert "fresh quote" in conf["note"]
        assert body["human_action_required"].endswith(f"hf delegate {cid} --adapter mock --confirm")

        assert MockGibworkAdapter(state_path=mock_state).balance.compare(__import__("decimal").Decimal("100.00")) == 0
        stored = harness.ok("contract_get", contract_id=cid)["contract"]
        assert stored["status"] == "ready"
        assert stored["attempts"][-1]["outcome"] == "prepared"

    def test_prepare_refused_for_wrong_state(self, harness: Harness) -> None:
        cid = harness.ok("contract_create", text="Summarize this.", force=True)["contract"]["id"]
        assert harness.err("delegate_prepare", contract_id=cid)["code"] == "INVALID_STATE"

    def test_prepare_never_submits_on_gibwork(self, tmp_path: Path) -> None:
        session = _happy()
        h, writes = _gibwork_harness(tmp_path, session)
        cid = h.ok("contract_create", text=REQ)["contract"]["id"]
        body = h.ok("delegate_prepare", contract_id=cid)
        assert body["backend"]["environment"] == "stage"
        assert body["task_id"] == P.PREPARE_RESULT["taskId"]
        assert body["last_valid_block_height"] == 424294625
        assert session.count("gibwork_task_create_prepare") == 1
        assert session.count("gibwork_task_create_submit") == 0
        assert session.closed
        # prepare is a write-side tool on the backend; the server was opened for it
        assert writes == [True]

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (P.ERROR_TOKEN_ACCOUNT, "MISSING_TOKEN_ACCOUNT"),
            (P.ERROR_AMOUNT_RANGE, "AMOUNT_OUT_OF_RANGE"),
            (P.ERROR_CREDENTIAL, "CREDENTIAL_ERROR"),
        ],
    )
    def test_adapter_errors_surface_as_envelopes(self, tmp_path: Path, payload: dict, code: str) -> None:
        session = _happy()
        session.handlers["gibwork_task_create_prepare"] = tool_error(payload)
        h, _ = _gibwork_harness(tmp_path, session)
        cid = h.ok("contract_create", text=REQ)["contract"]["id"]
        err = h.err("delegate_prepare", contract_id=cid)
        assert err["code"] == code
        assert err["message"]

    def test_backend_unreachable(self, tmp_path: Path) -> None:
        session = _happy()
        session.handlers["gibwork_wallet_status"] = raises(McpTransportError("exited", dispatched=True))
        h, _ = _gibwork_harness(tmp_path, session)
        assert h.err("wallet")["code"] == "NETWORK_ERROR"


class TestUncertainLock:
    def _locked(self, tmp_path: Path, session: FakeSession) -> tuple[Harness, str]:
        """Produce a locked contract through the service (the CLI path), then inspect via MCP."""
        h, _ = _gibwork_harness(tmp_path, session)
        session.handlers["gibwork_task_create_submit"] = raises(McpTransportError("timeout", dispatched=True))
        cid = h.ok("contract_create", text=REQ)["contract"]["id"]
        with h.service.open_delegation(cid) as ms:
            ms.prepare()
            with pytest.raises(Exception):
                ms.submit()
        return h, cid

    def test_locked_contract_view(self, tmp_path: Path) -> None:
        h, cid = self._locked(tmp_path, _happy())
        body = h.ok("contract_get", contract_id=cid)
        assert body["contract"]["status"] == "submit_uncertain"
        assert body["locked"] is True
        assert "unknown outcome" in body["lock_reason"]
        assert body["allowed_actions"] == [PREFIX + "contract_reconcile"]
        assert "hf contract reconcile" in body["human_action_required"]
        assert h.ok("contract_list")["contracts"][0]["locked"] is True

    def test_prepare_refused_while_locked(self, tmp_path: Path) -> None:
        session = _happy()
        h, cid = self._locked(tmp_path, session)
        before = session.count("gibwork_task_create_prepare")
        err = h.err("delegate_prepare", contract_id=cid)
        assert err["code"] == "CONTRACT_LOCKED"
        assert err["details"]["task_id"] == P.PREPARE_RESULT["taskId"]
        assert session.count("gibwork_task_create_prepare") == before
        assert session.count("gibwork_task_create_submit") == 1

    def test_reconcile_not_found_keeps_lock(self, tmp_path: Path) -> None:
        h, cid = self._locked(tmp_path, _happy())
        body = h.ok("contract_reconcile", contract_id=cid)
        assert body["outcome"] == "not_found"
        assert "hf contract resolve" in body["message"]
        assert body["contract"]["locked"] is True

    def test_reconcile_confirmed_clears_lock(self, tmp_path: Path) -> None:
        session = _happy()
        h, cid = self._locked(tmp_path, session)
        session.handlers["gibwork_task_list"] = returns({"results": [{**P.TASK_ITEM, "id": P.PREPARE_RESULT["taskId"]}]})
        body = h.ok("contract_reconcile", contract_id=cid)
        assert body["outcome"] == "confirmed"
        assert body["contract"]["contract"]["status"] == "delegated"
        assert body["contract"]["locked"] is False
        assert PREFIX + "submission_list" in body["contract"]["allowed_actions"]

    def test_reconcile_on_healthy_contract_is_error(self, harness: Harness) -> None:
        cid = harness.ok("contract_create", text=REQ)["contract"]["id"]
        assert harness.err("contract_reconcile", contract_id=cid)["code"] == "NOT_RECONCILABLE"

    def test_no_tool_can_clear_lock_manually(self, tmp_path: Path) -> None:
        h, cid = self._locked(tmp_path, _happy())
        names = {t.name for t in h.tools()}
        assert not any("resolve" in n for n in names)
        assert h.ok("contract_get", contract_id=cid)["locked"] is True


# -- inspection ------------------------------------------------------------------


class TestInspection:
    def _delegated(self, harness: Harness) -> str:
        cid = harness.ok("contract_create", text=REQ)["contract"]["id"]
        with harness.service.open_delegation(cid) as ms:
            ms.prepare()
            ms.submit()
        return cid

    def test_refresh_and_submissions_mock(self, harness: Harness, mock_state: Path) -> None:
        cid = self._delegated(harness)
        body = harness.ok("contract_refresh", contract_id=cid)
        assert body["contract"]["remote"]["total_submissions"] == 0
        assert body["backend"]["adapter"] == "mock"
        assert PREFIX + "submission_list" in body["allowed_actions"]

        task_id = body["contract"]["delegation"]["task_id"]
        MockGibworkAdapter(state_path=mock_state).seed_submission(task_id, content="proof", submitter="ann")
        listed = harness.ok("submission_list", contract_id=cid)
        assert listed["total"] == 1 and listed["task_id"] == task_id
        sub = listed["submissions"][0]
        assert sub["submitter"] == "ann"
        got = harness.ok("submission_get", contract_id=cid, submission_id=sub["id"])
        assert got["submission"]["content"] == "proof"
        assert harness.err("submission_get", contract_id=cid, submission_id="nope")["code"] == "NOT_FOUND"
        assert harness.ok("submission_list", contract_id=cid, status="approved")["total"] == 0

    def test_inspection_uses_read_only_backend(self, tmp_path: Path) -> None:
        session = _happy()
        h, writes = _gibwork_harness(tmp_path, session)
        cid = h.ok("contract_create", text=REQ)["contract"]["id"]
        with h.service.open_delegation(cid) as ms:
            ms.prepare()
            ms.submit()
        writes.clear()
        session.handlers["gibwork_task_list"] = returns({"results": [{**P.TASK_ITEM, "id": P.PREPARE_RESULT["taskId"], "totalSubmissions": 1}]})
        body = h.ok("contract_refresh", contract_id=cid)
        assert body["contract"]["status"] == "submitted"
        h.ok("submission_list", contract_id=cid)
        h.ok("submission_get", contract_id=cid, submission_id=P.SUBMISSION_ITEM["id"])
        h.ok("wallet")
        assert writes == [False, False, False, False]
        assert session.command[-1] == "--read-only"

    def test_not_delegated_errors(self, harness: Harness) -> None:
        cid = harness.ok("contract_create", text=REQ)["contract"]["id"]
        assert harness.err("contract_refresh", contract_id=cid)["code"] == "NOT_DELEGATED"
        assert harness.err("submission_list", contract_id=cid)["code"] == "NOT_DELEGATED"

    def test_wallet_view(self, harness: Harness) -> None:
        body = harness.ok("wallet")
        assert body["adapter"] == "mock" and body["writes_enabled"] is True


class TestSafetyAcrossAllTools:
    def test_no_tool_ever_calls_a_submit(self, tmp_path: Path) -> None:
        session = _happy()
        h, _ = _gibwork_harness(tmp_path, session)
        cid = h.ok("contract_create", text=REQ)["contract"]["id"]
        h.ok("classify", text=REQ)
        h.ok("contract_get", contract_id=cid)
        h.ok("contract_list")
        h.ok("delegate_prepare", contract_id=cid)
        h.ok("delegate_prepare", contract_id=cid)  # prepare twice is fine; still no submit
        h.err("contract_refresh", contract_id=cid)
        h.err("contract_reconcile", contract_id=cid)
        h.err("submission_list", contract_id=cid)
        h.ok("wallet")
        h.ok("status")
        called = {name for name, _, _ in session.calls}
        assert not any("submit" in n or "refund" in n for n in called), called
        assert not any(money for _, _, money in session.calls)
