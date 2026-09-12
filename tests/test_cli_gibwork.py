"""CLI flows against the gibwork adapter with an in-process fake session."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from fakes import payloads as P
from fakes.session import FakeSession, FakeSessionFactory, raises, returns, tool_error
from humanfallback import cli
from humanfallback.adapters import GibworkMcpAdapter
from humanfallback.adapters.mcp_client import McpTransportError
from humanfallback.cli import app

runner = CliRunner()
PHYSICAL = "Go to the hardware store and take a photo of the shelf."


def _run(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _run_json(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.stdout


def _create(*extra: str) -> dict:
    code, out = _run_json("contract", "create", PHYSICAL, "--json", *extra)
    assert code == 0, out
    return json.loads(out)


def _show(contract_id: str) -> dict:
    code, out = _run_json("contract", "show", contract_id, "--json")
    assert code == 0, out
    return json.loads(out)


def _happy() -> FakeSession:
    return FakeSession(
        {
            "gibwork_wallet_status": returns(P.WALLET_STATUS),
            "gibwork_task_list": returns(P.TASK_LIST_EMPTY),
            "gibwork_task_create_prepare": returns(P.PREPARE_RESULT),
            "gibwork_task_create_submit": returns(P.SUBMIT_RESULT),
            "gibwork_task_refund_prepare": lambda a: {**P.REFUND_PREPARE_RESULT, "taskId": a["taskId"]},
            "gibwork_task_refund_submit": lambda a: {"signature": "rs"},
            "gibwork_submission_list": returns(P.SUBMISSION_LIST),
            "gibwork_submission_get": returns(P.SUBMISSION_ITEM),
        }
    )


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch, data_dir) -> FakeSession:  # noqa: ANN001
    """Install a gibwork factory that uses the fake session; record writes flag."""
    fake = _happy()
    fake.writes_requested: list[bool] = []  # type: ignore[attr-defined]

    def factory(writes: bool) -> GibworkMcpAdapter:
        fake.writes_requested.append(writes)  # type: ignore[attr-defined]
        return GibworkMcpAdapter(
            command=["node", "bin.js"],
            profile="default",
            writes=writes,
            session_factory=FakeSessionFactory(fake),
        )

    monkeypatch.setitem(cli.ADAPTER_FACTORIES, "gibwork", factory)
    return fake


def _delegated_contract(session: FakeSession) -> dict:
    created = _create("--reward", "1.00")
    code, out = _run_json("delegate", created["id"], "--adapter", "gibwork", "--confirm", "--yes", "--json")
    assert code == 0, out
    return json.loads(out)


class TestDefaults:
    def test_mock_is_default_adapter(self, data_dir) -> None:  # noqa: ANN001
        created = _create()
        code, out = _run("delegate", created["id"])
        assert code == 0
        assert "backend: mock" in out

    def test_env_selects_gibwork(self, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_ADAPTER", "gibwork")
        code, out = _run("wallet")
        assert code == 0
        assert "gibwork" in out and "stage" in out and P.WALLET in out

    def test_status_shows_configured_adapter(self, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_ADAPTER", "gibwork")
        code, out = _run("status")
        assert code == 0
        assert "adapter:   gibwork" in out
        assert session.count("gibwork_wallet_status") == 0  # status never spawns the backend


class TestAnnounce:
    def test_wallet_command(self, session: FakeSession) -> None:
        code, out = _run("wallet", "--adapter", "gibwork")
        assert code == 0
        assert "profile:      default" in out
        assert "environment:  stage" in out
        assert f"wallet:       {P.WALLET}" in out
        assert session.writes_requested == [False]  # type: ignore[attr-defined]
        assert session.command[-1] == "--read-only"

    def test_dry_run_announces_before_prepare(self, session: FakeSession) -> None:
        created = _create("--reward", "1.00")
        code, out = _run("delegate", created["id"], "--adapter", "gibwork")
        assert code == 0, out
        announce = out.index("backend: gibwork")
        quote = out.index("total_debit:")
        assert announce < quote
        assert "profile=default" in out and "environment=stage" in out
        assert "real funds" in out
        assert session.count("gibwork_task_create_submit") == 0

    def test_dry_run_needs_write_server_but_never_submits(self, session: FakeSession) -> None:
        # prepare is a write-registered tool on the server, so the quote needs
        # --allow-writes; the safety property is that submit is never called.
        created = _create("--reward", "1.00")
        rc, _ = _run("delegate", created["id"], "--adapter", "gibwork")
        assert rc == 0
        assert session.writes_requested == [True]  # type: ignore[attr-defined]
        assert session.command[-1] == "--allow-writes"
        assert session.count("gibwork_task_create_prepare") == 1
        assert session.count("gibwork_task_create_submit") == 0


class TestApprovalGate:
    def test_yes_without_confirm_is_refused(self, session: FakeSession) -> None:
        created = _create("--reward", "1.00")
        code, out = _run("delegate", created["id"], "--adapter", "gibwork", "--yes")
        assert code == 2
        assert "requires --confirm" in out
        assert session.calls == []  # never even resolved the backend

    def test_confirm_without_tty_or_yes_aborts_before_submit(self, session: FakeSession) -> None:
        created = _create("--reward", "1.00")
        code, out = _run("delegate", created["id"], "--adapter", "gibwork", "--confirm")
        assert code == 1
        assert "interactive approval required" in out
        assert session.count("gibwork_task_create_prepare") == 1
        assert session.count("gibwork_task_create_submit") == 0
        shown = _show(created["id"])
        assert shown["status"] == "ready"
        assert shown["attempts"][0]["outcome"] == "prepared"

    def test_interactive_decline_aborts(self, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
        created = _create("--reward", "1.00")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        result = runner.invoke(app, ["delegate", created["id"], "--adapter", "gibwork", "--confirm"], input="n\n")
        assert result.exit_code == 1
        assert "aborted" in result.output
        assert session.count("gibwork_task_create_submit") == 0

    def test_interactive_accept_submits(self, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
        created = _create("--reward", "1.00")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        result = runner.invoke(app, ["delegate", created["id"], "--adapter", "gibwork", "--confirm"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "Fund 1.00 USDC from wallet" in result.output
        assert session.count("gibwork_task_create_submit") == 1

    def test_confirm_yes_submits_with_write_server(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        assert data["status"] == "delegated"
        assert data["delegation"]["adapter"] == "gibwork"
        assert data["delegation"]["task_id"] == P.PREPARE_RESULT["taskId"]
        assert data["delegation"]["signature"].startswith("5VfYmGB7")
        assert data["attempts"][-1]["outcome"] == "submitted"
        assert session.writes_requested == [True]  # type: ignore[attr-defined]
        assert session.command[-1] == "--allow-writes"
        assert session.count("gibwork_task_create_submit") == 1


class TestFailures:
    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (P.ERROR_TOKEN_ACCOUNT, "MISSING_TOKEN_ACCOUNT"),
            (P.ERROR_AMOUNT_RANGE, "AMOUNT_OUT_OF_RANGE"),
            (P.ERROR_CREDENTIAL, "CREDENTIAL_ERROR"),
        ],
    )
    def test_prepare_errors_are_reported(self, session: FakeSession, payload: dict, code: str) -> None:
        session.handlers["gibwork_task_create_prepare"] = tool_error(payload)
        created = _create("--reward", "1.00")
        rc, out = _run("delegate", created["id"], "--adapter", "gibwork")
        assert rc == 1
        assert code in out

    def test_submit_rejection_marks_attempt_failed(self, session: FakeSession) -> None:
        session.handlers["gibwork_task_create_submit"] = tool_error(P.ERROR_INSUFFICIENT)
        created = _create("--reward", "1.00")
        rc, out = _run("delegate", created["id"], "--adapter", "gibwork", "--confirm", "--yes")
        assert rc == 1
        assert "INSUFFICIENT_FUNDS" in out
        shown = _show(created["id"])
        assert shown["status"] == "ready"
        assert shown["attempts"][-1]["outcome"] == "failed"

    def test_expired_confirmation_reported(self, session: FakeSession) -> None:
        session.handlers["gibwork_task_create_submit"] = tool_error(P.ERROR_CONFIRMATION_EXPIRED)
        created = _create("--reward", "1.00")
        rc, out = _run("delegate", created["id"], "--adapter", "gibwork", "--confirm", "--yes")
        assert rc == 1
        assert "CONFIRMATION_EXPIRED" in out
        assert _show(created["id"])["status"] == "ready"

    def test_backend_unreachable(self, session: FakeSession) -> None:
        session.handlers["gibwork_wallet_status"] = raises(McpTransportError("exited", dispatched=True))
        created = _create("--reward", "1.00")
        rc, out = _run("delegate", created["id"], "--adapter", "gibwork")
        assert rc == 1
        assert "NETWORK_ERROR" in out


class TestUncertainSubmit:
    def _make_uncertain(self, session: FakeSession) -> dict:
        session.handlers["gibwork_task_create_submit"] = raises(
            McpTransportError("timed out", dispatched=True)
        )
        created = _create("--reward", "1.00")
        rc, out = _run("delegate", created["id"], "--adapter", "gibwork", "--confirm", "--yes")
        assert rc == 22, out
        assert "submit_uncertain" in out
        assert "Do not resubmit" in out
        return _show(created["id"])

    def test_enters_uncertain_state(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        assert shown["status"] == "submit_uncertain"
        attempt = shown["attempts"][-1]
        assert attempt["outcome"] == "ambiguous"
        assert attempt["task_id"] == P.PREPARE_RESULT["taskId"]
        assert attempt["confirmation_id"] == P.PREPARE_RESULT["confirmationId"]
        assert session.count("gibwork_task_create_submit") == 1

    def test_further_submits_refused(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        session.handlers["gibwork_task_create_submit"] = returns(P.SUBMIT_RESULT)
        rc, out = _run("delegate", shown["id"], "--adapter", "gibwork", "--confirm", "--yes")
        assert rc == 1
        assert "reconcile" in out
        rc, out = _run("refund", shown["id"], "--adapter", "gibwork", "--confirm", "--yes")
        assert rc == 1
        assert "reconcile" in out
        assert session.count("gibwork_task_create_submit") == 1
        assert session.count("gibwork_task_refund_submit") == 0

    def test_reconcile_found_promotes_to_delegated(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        found = {**P.TASK_ITEM, "id": P.PREPARE_RESULT["taskId"]}
        session.handlers["gibwork_task_list"] = returns({"results": [found]})
        rc, out = _run("contract", "reconcile", shown["id"], "--adapter", "gibwork")
        assert rc == 0, out
        assert "contract is delegated" in out
        after = _show(shown["id"])
        assert after["status"] == "delegated"
        assert after["delegation"]["task_id"] == P.PREPARE_RESULT["taskId"]
        assert after["attempts"][-1]["outcome"] == "reconciled_confirmed"

    def test_reconcile_not_found_keeps_lock(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        rc, out = _run("contract", "reconcile", shown["id"], "--adapter", "gibwork")
        assert rc == 1
        assert "not found" in out and "resolve" in out
        assert _show(shown["id"])["status"] == "submit_uncertain"

    def test_reconcile_uses_read_only_server(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        session.writes_requested.clear()  # type: ignore[attr-defined]
        _run("contract", "reconcile", shown["id"], "--adapter", "gibwork")
        assert session.writes_requested == [False]  # type: ignore[attr-defined]

    def test_reconcile_with_other_adapter_refused(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        rc, out = _run("contract", "reconcile", shown["id"], "--adapter", "mock")
        assert rc == 1
        assert "same adapter" in out

    def test_manual_resolve_returns_to_ready(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        rc, out = _run("contract", "resolve", shown["id"], "--outcome", "not-created")
        assert rc == 0, out
        after = _show(shown["id"])
        assert after["status"] == "ready"
        assert after["attempts"][-1]["outcome"] == "reconciled_not_found"

    def test_manual_resolve_wrong_outcome_refused(self, session: FakeSession) -> None:
        shown = self._make_uncertain(session)
        rc, out = _run("contract", "resolve", shown["id"], "--outcome", "not-refunded")
        assert rc == 1
        assert "does not apply" in out
        rc, out = _run("contract", "resolve", shown["id"], "--outcome", "bogus")
        assert rc == 2

    def test_resolve_without_uncertainty_refused(self, session: FakeSession) -> None:
        created = _create()
        rc, out = _run("contract", "resolve", created["id"], "--outcome", "not-created")
        assert rc == 1


class TestRefund:
    def test_dry_run_shows_payload(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        rc, out = _run("refund", data["id"], "--adapter", "gibwork")
        assert rc == 0, out
        assert "refund_amount:     10.00 USDC" in out
        assert "backend payload:" in out
        assert P.REFUND_PREPARE_RESULT["confirmationId"] in out
        assert session.count("gibwork_task_refund_submit") == 0

    def test_confirm_yes_refunds_and_closes(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        rc, out = _run_json("refund", data["id"], "--adapter", "gibwork", "--confirm", "--yes", "--json")
        assert rc == 0, out
        after = json.loads(out)
        assert after["status"] == "closed"
        assert after["refund"]["signature"] == "rs"
        assert after["attempts"][-1]["operation"] == "task_refund"
        assert session.count("gibwork_task_refund_submit") == 1

    def test_refund_ambiguous_locks(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        session.handlers["gibwork_task_refund_submit"] = raises(
            McpTransportError("exited", dispatched=True)
        )
        rc, out = _run("refund", data["id"], "--adapter", "gibwork", "--confirm", "--yes")
        assert rc == 22
        assert _show(data["id"])["status"] == "submit_uncertain"

        closed = {**P.TASK_ITEM, "id": P.PREPARE_RESULT["taskId"], "status": "CLOSED", "isOpen": False}
        session.handlers["gibwork_task_list"] = returns({"results": [closed]})
        rc, out = _run("contract", "reconcile", data["id"], "--adapter", "gibwork")
        assert rc == 0, out
        assert _show(data["id"])["status"] == "closed"

    def test_refund_with_wrong_adapter_refused(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        rc, out = _run("refund", data["id"], "--adapter", "mock")
        assert rc == 1
        assert "same adapter" in out

    def test_refund_undelegated_refused(self, session: FakeSession) -> None:
        created = _create()
        rc, out = _run("refund", created["id"], "--adapter", "gibwork")
        assert rc == 1
        assert "only delegated" in out


class TestInspection:
    def test_submissions_list_and_show(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        rc, out = _run("submissions", data["id"], "--adapter", "gibwork")
        assert rc == 0, out
        assert "meme_worker" in out
        rc, out = _run("submissions", data["id"], "--adapter", "gibwork", "--status", "approved")
        assert session.calls[-1][1]["status"] == "approved"

        rc, out = _run("submission", data["id"], P.SUBMISSION_ITEM["id"], "--adapter", "gibwork")
        assert rc == 0, out
        assert "status:     pending" in out
        assert "https://cdn.gib.work/media/m1.png" in out

    def test_submission_missing(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        session.handlers["gibwork_submission_get"] = tool_error(P.ERROR_NOT_FOUND)
        rc, out = _run("submission", data["id"], "nope", "--adapter", "gibwork")
        assert rc == 1
        assert "not found" in out

    def test_refresh_records_snapshot_and_promotes(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        remote = {**P.TASK_ITEM, "id": P.PREPARE_RESULT["taskId"], "totalSubmissions": 2}
        session.handlers["gibwork_task_list"] = returns({"results": [remote]})
        rc, out = _run_json("contract", "refresh", data["id"], "--adapter", "gibwork", "--json")
        assert rc == 0, out
        after = json.loads(out)
        assert after["status"] == "submitted"
        assert after["remote"]["total_submissions"] == 2
        assert after["remote"]["is_open"] is True

    def test_refresh_missing_task(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        rc, out = _run("contract", "refresh", data["id"], "--adapter", "gibwork")
        assert rc == 1
        assert "not found" in out

    def test_inspection_uses_read_only(self, session: FakeSession) -> None:
        data = _delegated_contract(session)
        session.writes_requested.clear()  # type: ignore[attr-defined]
        _run("submissions", data["id"], "--adapter", "gibwork")
        _run("contract", "refresh", data["id"], "--adapter", "gibwork")
        assert session.writes_requested == [False, False]  # type: ignore[attr-defined]
