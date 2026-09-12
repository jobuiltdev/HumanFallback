"""GibworkMcpAdapter against an in-process fake session. No process, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import payloads as P
from fakes.session import FakeSession, FakeSessionFactory, raises, returns, tool_error
from humanfallback.adapters import AdapterError, AmbiguousSubmit, ErrorCode, GibworkMcpAdapter
from humanfallback.adapters.gibwork import build_server_command, resolve_gibwork_command
from humanfallback.adapters.mcp_client import McpProtocolError, McpTransportError
from humanfallback.models import Reward, TaskContract

CMD = ["node", "bin.js"]


def _adapter(session: FakeSession, *, writes: bool = True, **kw) -> GibworkMcpAdapter:
    return GibworkMcpAdapter(
        command=CMD, writes=writes, session_factory=FakeSessionFactory(session), **kw
    )


def _happy_session() -> FakeSession:
    return FakeSession(
        {
            "gibwork_wallet_status": returns(P.WALLET_STATUS),
            "gibwork_task_list": returns(P.TASK_LIST),
            "gibwork_task_create_prepare": returns(P.PREPARE_RESULT),
            "gibwork_task_create_submit": returns(P.SUBMIT_RESULT),
            "gibwork_task_refund_prepare": returns(P.REFUND_PREPARE_RESULT),
            "gibwork_task_refund_submit": returns({"taskId": P.TASK_ITEM["id"], "signature": "refundsig"}),
            "gibwork_submission_list": returns(P.SUBMISSION_LIST),
            "gibwork_submission_get": returns(P.SUBMISSION_ITEM),
        }
    )


class TestCommand:
    def test_read_only_by_default(self) -> None:
        cmd = build_server_command(CMD, profile=None, environment=None, writes=False)
        assert cmd == ["node", "bin.js", "mcp", "serve", "--read-only"]

    def test_writes_profile_and_environment(self) -> None:
        cmd = build_server_command(CMD, profile="stage", environment="stage", writes=True)
        assert cmd == [
            "node", "bin.js", "--profile", "stage", "--environment", "stage",
            "mcp", "serve", "--allow-writes",
        ]

    def test_adapter_passes_command_to_session(self) -> None:
        session = _happy_session()
        adapter = _adapter(session, writes=False, profile="p")
        adapter.wallet_status()
        assert session.command == ["node", "bin.js", "--profile", "p", "mcp", "serve", "--read-only"]

    def test_explicit_js_path_uses_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)
        assert resolve_gibwork_command("/x/bin.js") == ["/usr/bin/node", str(Path("/x/bin.js"))]

    def test_explicit_binary_used_verbatim(self) -> None:
        assert resolve_gibwork_command("/x/gibwork") == [str(Path("/x/gibwork"))]

    def test_missing_cli_is_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(AdapterError) as exc:
            resolve_gibwork_command(None)
        assert exc.value.code is ErrorCode.CONFIG_ERROR


class TestInspection:
    def test_wallet_status_populates_identity(self) -> None:
        session = _happy_session()
        adapter = _adapter(session, writes=False)
        status = adapter.wallet_status()
        assert status.environment == "stage"
        assert adapter.environment == "stage"
        assert adapter.wallet_address == P.WALLET
        assert session.started

    def test_connect_caches(self) -> None:
        session = _happy_session()
        adapter = _adapter(session, writes=False)
        adapter.connect()
        adapter.connect()
        assert session.count("gibwork_wallet_status") == 1

    def test_list_and_get_task(self) -> None:
        adapter = _adapter(_happy_session(), writes=False)
        tasks = adapter.list_tasks()
        assert [t.id for t in tasks] == [P.TASK_ITEM["id"]]
        assert adapter.get_task(P.TASK_ITEM["id"]) is not None
        assert adapter.get_task("missing") is None

    def test_list_submissions_passes_filter(self) -> None:
        session = _happy_session()
        adapter = _adapter(session, writes=False)
        subs = adapter.list_submissions("t1", status="pending")
        assert subs[0].submitter == "meme_worker"
        name, args, money = session.calls[-1]
        assert (name, money) == ("gibwork_submission_list", False)
        assert args == {"taskId": "t1", "pageAll": True, "status": "pending"}

    def test_get_submission_not_found_returns_none(self) -> None:
        session = _happy_session()
        session.handlers["gibwork_submission_get"] = tool_error(P.ERROR_NOT_FOUND)
        assert _adapter(session, writes=False).get_submission("t", "s") is None

    def test_read_only_adapter_refuses_prepare(self, contract: TaskContract) -> None:
        adapter = _adapter(_happy_session(), writes=False)
        with pytest.raises(AdapterError) as exc:
            adapter.prepare_task(contract)
        assert exc.value.code is ErrorCode.NOT_SUPPORTED

    def test_close_closes_session(self) -> None:
        session = _happy_session()
        with _adapter(session, writes=False) as adapter:
            adapter.wallet_status()
        assert session.closed


class TestPrepare:
    def test_maps_arguments_and_result(self, contract: TaskContract) -> None:
        session = _happy_session()
        adapter = _adapter(session)
        result = adapter.prepare_task(contract)
        name, args, money = session.calls[-1]
        assert (name, money) == ("gibwork_task_create_prepare", False)
        assert args["title"] == contract.title
        assert args["content"] == contract.description
        assert args["tags"] == ["errand", "photo"]
        assert args["payment"] == {"mintAddress": P.USDC, "amount": "5.00"}
        assert args["minSubmissionAmount"] == "5.00"
        assert args["deadline"] is None
        assert args["allowOnlyVerifiedSubmissions"] is False
        assert result.confirmation_id == P.PREPARE_RESULT["confirmationId"]
        assert result.payment_quote.total_debit == "1.00"

    def test_local_amount_check_never_calls_server(self, contract: TaskContract) -> None:
        session = _happy_session()
        adapter = _adapter(session)
        low = contract.model_copy(update={"reward": Reward(amount="0.50")})
        with pytest.raises(AdapterError) as exc:
            adapter.prepare_task(low)
        assert exc.value.code is ErrorCode.AMOUNT_OUT_OF_RANGE
        assert session.count("gibwork_task_create_prepare") == 0

    def test_local_mint_check(self, contract: TaskContract) -> None:
        adapter = _adapter(_happy_session())
        other = contract.model_copy(
            update={"reward": Reward(amount="1.00", mint_address="So11111111111111111111111111111111111111112")}
        )
        with pytest.raises(AdapterError) as exc:
            adapter.prepare_task(other)
        assert exc.value.code is ErrorCode.UNSUPPORTED_MINT

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (P.ERROR_TOKEN_ACCOUNT, ErrorCode.MISSING_TOKEN_ACCOUNT),
            (P.ERROR_AMOUNT_RANGE, ErrorCode.AMOUNT_OUT_OF_RANGE),
            (P.ERROR_INSUFFICIENT, ErrorCode.INSUFFICIENT_FUNDS),
            (P.ERROR_UNSUPPORTED_MINT, ErrorCode.UNSUPPORTED_MINT),
            (P.ERROR_CREDENTIAL, ErrorCode.CREDENTIAL_ERROR),
        ],
    )
    def test_server_errors_translated(self, contract: TaskContract, payload: dict, code: ErrorCode) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_prepare"] = tool_error(payload)
        with pytest.raises(AdapterError) as exc:
            _adapter(session).prepare_task(contract)
        assert exc.value.code is code

    def test_transport_failure_on_prepare_is_network_error(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_prepare"] = raises(
            McpTransportError("boom", dispatched=True)
        )
        with pytest.raises(AdapterError) as exc:
            _adapter(session).prepare_task(contract)
        assert exc.value.code is ErrorCode.NETWORK_ERROR

    def test_server_start_failure(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.start = lambda: (_ for _ in ()).throw(McpTransportError("no exe", dispatched=False))  # type: ignore[method-assign]
        with pytest.raises(AdapterError) as exc:
            _adapter(session).wallet_status()
        assert exc.value.code is ErrorCode.NETWORK_ERROR


class TestSubmit:
    def test_submits_once_and_maps(self, contract: TaskContract) -> None:
        session = _happy_session()
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        result = adapter.submit_task(prepared.confirmation_id)
        name, args, money = session.calls[-1]
        assert (name, money) == ("gibwork_task_create_submit", True)
        assert args == {"confirmationId": prepared.confirmation_id}
        assert result.task_id == prepared.task_id
        assert result.intent_id == prepared.intent_id
        assert result.signature.startswith("5VfYmGB7")
        assert result.status == "fulfilled"

    def test_second_submit_refused_locally(self, contract: TaskContract) -> None:
        session = _happy_session()
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        adapter.submit_task(prepared.confirmation_id)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID
        assert session.count("gibwork_task_create_submit") == 1

    def test_unprepared_confirmation_refused_locally(self) -> None:
        session = _happy_session()
        with pytest.raises(AdapterError) as exc:
            _adapter(session).submit_task("00000000-0000-4000-8000-000000000000")
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID
        assert session.count("gibwork_task_create_submit") == 0

    def test_wrong_operation_refused_locally(self, contract: TaskContract) -> None:
        session = _happy_session()
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_refund(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID
        assert session.count("gibwork_task_refund_submit") == 0

    def test_expired_confirmation_from_server(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_submit"] = tool_error(P.ERROR_CONFIRMATION_EXPIRED)
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_EXPIRED

    def test_insufficient_funds_on_submit(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_submit"] = tool_error(P.ERROR_INSUFFICIENT)
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.INSUFFICIENT_FUNDS

    def test_dispatched_transport_failure_is_ambiguous(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_submit"] = raises(
            McpTransportError("timed out", dispatched=True)
        )
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AmbiguousSubmit) as exc:
            adapter.submit_task(prepared.confirmation_id)
        err = exc.value
        assert err.code is ErrorCode.AMBIGUOUS_SUBMIT
        assert err.operation == "task_create"
        assert err.confirmation_id == prepared.confirmation_id
        assert err.task_id == prepared.task_id
        assert err.intent_id == prepared.intent_id
        assert "timed out" in (err.cause or "")
        assert session.count("gibwork_task_create_submit") == 1

    def test_ambiguous_submit_is_never_retried(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_submit"] = raises(
            McpTransportError("timed out", dispatched=True)
        )
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AmbiguousSubmit):
            adapter.submit_task(prepared.confirmation_id)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID
        assert session.count("gibwork_task_create_submit") == 1

    def test_protocol_error_on_submit_is_ambiguous(self, contract: TaskContract) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_create_submit"] = raises(McpProtocolError(-32000, "odd"))
        adapter = _adapter(session)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AmbiguousSubmit):
            adapter.submit_task(prepared.confirmation_id)


class TestRefund:
    def test_prepare_and_submit(self) -> None:
        session = _happy_session()
        adapter = _adapter(session)
        prepared = adapter.prepare_refund(P.TASK_ITEM["id"])
        assert session.calls[-1][1] == {"taskId": P.TASK_ITEM["id"]}
        assert prepared.refund_amount == "10.00"
        result = adapter.submit_refund(prepared.confirmation_id)
        name, args, money = session.calls[-1]
        assert (name, money) == ("gibwork_task_refund_submit", True)
        assert result.task_id == P.TASK_ITEM["id"]
        assert result.signature == "refundsig"

    def test_refund_ambiguous(self) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_refund_submit"] = raises(
            McpTransportError("exited", dispatched=True)
        )
        adapter = _adapter(session)
        prepared = adapter.prepare_refund(P.TASK_ITEM["id"])
        with pytest.raises(AmbiguousSubmit) as exc:
            adapter.submit_refund(prepared.confirmation_id)
        assert exc.value.operation == "task_refund"

    def test_refund_prepare_without_confirmation_is_protocol_error(self) -> None:
        session = _happy_session()
        session.handlers["gibwork_task_refund_prepare"] = returns({"unexpected": True})
        with pytest.raises(AdapterError) as exc:
            _adapter(session).prepare_refund("t")
        assert exc.value.code is ErrorCode.PROTOCOL_ERROR

    def test_read_only_refuses_refund(self) -> None:
        with pytest.raises(AdapterError) as exc:
            _adapter(_happy_session(), writes=False).prepare_refund("t")
        assert exc.value.code is ErrorCode.NOT_SUPPORTED
