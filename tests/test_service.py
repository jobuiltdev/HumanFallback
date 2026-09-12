"""Service layer: the single home of delegation rules and the uncertain-submit lock."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from fakes import payloads as P
from fakes.session import FakeSession, FakeSessionFactory, raises, returns
from humanfallback.adapters import AdapterError, AmbiguousSubmit, ErrorCode, GibworkMcpAdapter, MockGibworkAdapter
from humanfallback.adapters.mcp_client import McpTransportError
from humanfallback.models import AttemptOutcome, ContractStatus
from humanfallback.service import Service, ServiceCode, ServiceError, error_envelope
from humanfallback.store import ContractStore

REQ = "Go to the hardware store and take a photo of the shelf."


@pytest.fixture
def mock_service(tmp_path: Path) -> Service:
    state = tmp_path / "mock.json"
    return Service(
        store_factory=lambda: ContractStore(tmp_path / "hf.db"),
        adapter_factory=lambda writes: MockGibworkAdapter(state_path=state),
        adapter_name="mock",
    )


def _gibwork_service(tmp_path: Path, session: FakeSession) -> Service:
    def factory(writes: bool) -> GibworkMcpAdapter:
        return GibworkMcpAdapter(
            command=["node", "bin.js"], writes=writes, session_factory=FakeSessionFactory(session)
        )

    return Service(
        store_factory=lambda: ContractStore(tmp_path / "hf.db"),
        adapter_factory=factory,
        adapter_name="gibwork",
    )


def _happy() -> FakeSession:
    return FakeSession(
        {
            "gibwork_wallet_status": returns(P.WALLET_STATUS),
            "gibwork_task_list": returns(P.TASK_LIST_EMPTY),
            "gibwork_task_create_prepare": returns(P.PREPARE_RESULT),
            "gibwork_task_create_submit": returns(P.SUBMIT_RESULT),
            "gibwork_submission_list": returns(P.SUBMISSION_LIST),
            "gibwork_submission_get": returns(P.SUBMISSION_ITEM),
        }
    )


class TestLocal:
    def test_create_get_list_stats(self, mock_service: Service) -> None:
        c = mock_service.create_contract(REQ, reward="2.00", tags=["a"])
        assert c.status is ContractStatus.READY
        assert mock_service.get_contract(c.id).id == c.id
        assert [x.id for x in mock_service.list_contracts()] == [c.id]
        assert mock_service.list_contracts(status=ContractStatus.DRAFT) == []
        stats = mock_service.stats()
        assert stats.counts == {"ready": 1}
        assert stats.adapter == "mock"

    def test_missing_contract(self, mock_service: Service) -> None:
        with pytest.raises(ServiceError) as exc:
            mock_service.get_contract("nope")
        assert exc.value.code is ServiceCode.NOT_FOUND

    def test_agent_capable_refused_unless_forced(self, mock_service: Service) -> None:
        with pytest.raises(ServiceError) as exc:
            mock_service.create_contract("Refactor the module and write tests.")
        assert exc.value.code is ServiceCode.AGENT_CAPABLE
        assert "classification" in exc.value.details
        c = mock_service.create_contract("Refactor the module and write tests.", force=True)
        assert c.status is ContractStatus.DRAFT

    def test_validation_errors_are_readable(self, mock_service: Service) -> None:
        with pytest.raises(ServiceError) as exc:
            mock_service.create_contract(REQ, reward="5")
        assert exc.value.code is ServiceCode.VALIDATION
        assert "two decimals" in exc.value.message
        with pytest.raises(ServiceError):
            mock_service.create_contract(REQ, tags=["a", "b", "c", "d"])

    def test_error_envelope_shape(self) -> None:
        env = error_envelope(ServiceError(ServiceCode.NOT_FOUND, "gone", details={"id": "x"}))
        assert env == {"error": {"code": "NOT_FOUND", "message": "gone", "details": {"id": "x"}}}
        env = error_envelope(AdapterError(ErrorCode.NETWORK_ERROR, "down"))
        assert env["error"]["code"] == "NETWORK_ERROR"


class TestDelegationSession:
    def test_prepare_records_attempt_and_moves_nothing(self, mock_service: Service) -> None:
        c = mock_service.create_contract(REQ, reward="3.00")
        with mock_service.open_delegation(c.id) as session:
            assert session.backend.adapter == "mock"
            prepared = session.prepare()
            assert prepared.payment_quote.total_debit == "3.00"
            assert session.prepare() is prepared  # idempotent within a session
        stored = mock_service.get_contract(c.id)
        assert stored.status is ContractStatus.READY
        assert stored.attempts[-1].outcome is AttemptOutcome.PREPARED
        assert stored.attempts[-1].confirmation_id == prepared.confirmation_id
        assert MockGibworkAdapter(state_path=None).balance == Decimal("100.00")

    def test_submit_requires_prepare(self, mock_service: Service) -> None:
        c = mock_service.create_contract(REQ)
        with mock_service.open_delegation(c.id) as session:
            with pytest.raises(ServiceError) as exc:
                session.submit()
            assert exc.value.code is ServiceCode.INVALID_STATE

    def test_submit_once(self, mock_service: Service) -> None:
        c = mock_service.create_contract(REQ, reward="3.00")
        with mock_service.open_delegation(c.id) as session:
            session.prepare()
            result = session.submit()
            with pytest.raises(ServiceError):
                session.submit()
        stored = mock_service.get_contract(c.id)
        assert stored.status is ContractStatus.DELEGATED
        assert stored.delegation is not None and stored.delegation.task_id == result.task_id
        assert stored.attempts[-1].outcome is AttemptOutcome.SUBMITTED

    def test_open_refuses_wrong_state(self, mock_service: Service) -> None:
        c = mock_service.create_contract("Refactor the module.", force=True)
        with pytest.raises(ServiceError) as exc:
            mock_service.open_delegation(c.id)
        assert exc.value.code is ServiceCode.INVALID_STATE

    def test_failed_submit_is_retryable(self, tmp_path: Path) -> None:
        state = tmp_path / "mock.json"
        svc = Service(
            store_factory=lambda: ContractStore(tmp_path / "hf.db"),
            adapter_factory=lambda writes: MockGibworkAdapter(state_path=state, balance=Decimal("1.00")),
            adapter_name="mock",
        )
        c = svc.create_contract(REQ, reward="5.00")
        with svc.open_delegation(c.id) as session:
            session.prepare()
            with pytest.raises(AdapterError) as exc:
                session.submit()
            assert exc.value.code is ErrorCode.INSUFFICIENT_FUNDS
        stored = svc.get_contract(c.id)
        assert stored.status is ContractStatus.READY
        assert stored.attempts[-1].outcome is AttemptOutcome.FAILED
        svc.open_delegation(c.id).close()  # not locked

    def test_ambiguous_submit_locks(self, tmp_path: Path) -> None:
        session = _happy()
        session.handlers["gibwork_task_create_submit"] = raises(McpTransportError("timeout", dispatched=True))
        svc = _gibwork_service(tmp_path, session)
        c = svc.create_contract(REQ)
        with svc.open_delegation(c.id) as ms:
            ms.prepare()
            with pytest.raises(AmbiguousSubmit):
                ms.submit()
        stored = svc.get_contract(c.id)
        assert stored.status is ContractStatus.SUBMIT_UNCERTAIN
        assert stored.uncertain_attempt is not None
        with pytest.raises(ServiceError) as exc:
            svc.open_delegation(c.id)
        assert exc.value.code is ServiceCode.CONTRACT_LOCKED
        assert exc.value.details["task_id"] == P.PREPARE_RESULT["taskId"]
        with pytest.raises(ServiceError) as exc:
            svc.open_refund(c.id)
        assert exc.value.code is ServiceCode.CONTRACT_LOCKED
        assert session.count("gibwork_task_create_submit") == 1

    def test_reconcile_and_resolve(self, tmp_path: Path) -> None:
        session = _happy()
        session.handlers["gibwork_task_create_submit"] = raises(McpTransportError("timeout", dispatched=True))
        svc = _gibwork_service(tmp_path, session)
        c = svc.create_contract(REQ)
        with svc.open_delegation(c.id) as ms:
            ms.prepare()
            with pytest.raises(AmbiguousSubmit):
                ms.submit()

        result = svc.reconcile(c.id)
        assert result.outcome == "not_found"
        assert svc.get_contract(c.id).status is ContractStatus.SUBMIT_UNCERTAIN

        found = {**P.TASK_ITEM, "id": P.PREPARE_RESULT["taskId"]}
        session.handlers["gibwork_task_list"] = returns({"results": [found]})
        result = svc.reconcile(c.id)
        assert result.outcome == "confirmed"
        assert result.contract.status is ContractStatus.DELEGATED

        with pytest.raises(ServiceError) as exc:
            svc.reconcile(c.id)
        assert exc.value.code is ServiceCode.NOT_RECONCILABLE

    def test_resolve_returns_to_origin(self, tmp_path: Path) -> None:
        session = _happy()
        session.handlers["gibwork_task_create_submit"] = raises(McpTransportError("timeout", dispatched=True))
        svc = _gibwork_service(tmp_path, session)
        c = svc.create_contract(REQ)
        with svc.open_delegation(c.id) as ms:
            ms.prepare()
            with pytest.raises(AmbiguousSubmit):
                ms.submit()
        with pytest.raises(ServiceError):
            svc.resolve_uncertain(c.id, "not-refunded")
        with pytest.raises(ServiceError):
            svc.resolve_uncertain(c.id, "bogus")
        resolved = svc.resolve_uncertain(c.id, "not-created")
        assert resolved.status is ContractStatus.READY
        assert resolved.attempts[-1].outcome is AttemptOutcome.RECONCILED_NOT_FOUND

    def test_reconcile_requires_same_adapter(self, tmp_path: Path, mock_service: Service) -> None:
        session = _happy()
        session.handlers["gibwork_task_create_submit"] = raises(McpTransportError("timeout", dispatched=True))
        svc = _gibwork_service(tmp_path, session)
        c = svc.create_contract(REQ)
        with svc.open_delegation(c.id) as ms:
            ms.prepare()
            with pytest.raises(AmbiguousSubmit):
                ms.submit()
        other = Service(
            store_factory=lambda: ContractStore(tmp_path / "hf.db"),
            adapter_factory=lambda writes: MockGibworkAdapter(state_path=None),
            adapter_name="mock",
        )
        with pytest.raises(ServiceError) as exc:
            other.reconcile(c.id)
        assert exc.value.code is ServiceCode.ADAPTER_MISMATCH


class TestBackendReads:
    def test_refresh_and_submissions(self, mock_service: Service) -> None:
        c = mock_service.create_contract(REQ)
        with pytest.raises(ServiceError) as exc:
            mock_service.refresh(c.id)
        assert exc.value.code is ServiceCode.NOT_DELEGATED
        with mock_service.open_delegation(c.id) as s:
            s.prepare()
            s.submit()
        contract, backend = mock_service.refresh(c.id)
        assert backend.adapter == "mock"
        assert contract.remote is not None and contract.remote.total_submissions == 0
        assert contract.status is ContractStatus.DELEGATED

        adapter = MockGibworkAdapter(state_path=None)
        items, _, task_id = mock_service.list_submissions(c.id)
        assert items == [] and task_id == contract.delegation.task_id
        item, _ = mock_service.get_submission(c.id, "nope")
        assert item is None

    def test_refresh_promotes_when_submissions_exist(self, tmp_path: Path) -> None:
        state = tmp_path / "mock.json"
        svc = Service(
            store_factory=lambda: ContractStore(tmp_path / "hf.db"),
            adapter_factory=lambda writes: MockGibworkAdapter(state_path=state),
            adapter_name="mock",
        )
        c = svc.create_contract(REQ)
        with svc.open_delegation(c.id) as s:
            s.prepare()
            s.submit()
        MockGibworkAdapter(state_path=state).seed_submission(svc.get_contract(c.id).delegation.task_id, content="done")
        contract, _ = svc.refresh(c.id)
        assert contract.status is ContractStatus.SUBMITTED
        items, _, _ = svc.list_submissions(c.id, status="pending")
        assert len(items) == 1
        item, _ = svc.get_submission(c.id, items[0].id)
        assert item is not None and item.content == "done"

    def test_adapter_errors_propagate_and_close(self, tmp_path: Path) -> None:
        session = _happy()
        session.handlers["gibwork_wallet_status"] = raises(McpTransportError("exited", dispatched=True))
        svc = _gibwork_service(tmp_path, session)
        with pytest.raises(AdapterError) as exc:
            svc.backend_info()
        assert exc.value.code is ErrorCode.NETWORK_ERROR
        assert session.closed
