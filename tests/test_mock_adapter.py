from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from humanfallback.adapters import AdapterError, ErrorCode, MockGibworkAdapter
from humanfallback.adapters.mock import CONFIRMATION_TTL
from humanfallback.models import USDC_MINT, Reward, TaskContract

from conftest import FakeClock


def _with_reward(contract: TaskContract, **kwargs) -> TaskContract:
    return contract.model_copy(update={"reward": Reward(**kwargs)})


class TestPrepare:
    def test_returns_quote_and_ids(self, adapter: MockGibworkAdapter, contract: TaskContract, clock: FakeClock) -> None:
        result = adapter.prepare_task(contract)
        assert len(result.confirmation_id) == 36
        assert len(result.intent_id) == 36
        assert len(result.task_id) == 36
        assert result.wallet_address == adapter.wallet_address
        assert result.environment == "mock"
        assert result.created_at == clock.now
        assert result.expires_at == clock.now + CONFIRMATION_TTL
        q = result.payment_quote
        assert q.token.mint_address == USDC_MINT
        assert q.token.symbol == "USDC"
        assert q.token.decimals == 6
        assert q.funding_amount == "5.00"
        assert q.platform_fee.percent == 0
        assert q.platform_fee.amount == "0.00"
        assert q.total_debit == "5.00"
        assert "approval" in result.next_action

    def test_does_not_debit_balance(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        adapter.prepare_task(contract)
        assert adapter.balance == Decimal("100.00")
        assert adapter.list_tasks() == []

    def test_rejects_below_minimum(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        with pytest.raises(AdapterError, match="between 1.00 and 100000.00") as exc:
            adapter.prepare_task(_with_reward(contract, amount="0.50"))
        assert exc.value.code is ErrorCode.AMOUNT_OUT_OF_RANGE

    def test_rejects_above_maximum(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        with pytest.raises(AdapterError, match="between"):
            adapter.prepare_task(_with_reward(contract, amount="100000.01"))

    def test_accepts_boundaries(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        adapter.prepare_task(_with_reward(contract, amount="1.00"))
        adapter.prepare_task(_with_reward(contract, amount="100000.00"))

    def test_rejects_unsupported_mint(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        with pytest.raises(AdapterError) as exc:
            adapter.prepare_task(_with_reward(contract, amount="1.00", mint_address="So11111111111111111111111111111111111111112"))
        assert exc.value.code is ErrorCode.UNSUPPORTED_MINT

    def test_rejects_missing_token_account(self, contract: TaskContract) -> None:
        adapter = MockGibworkAdapter(has_token_account=False)
        with pytest.raises(AdapterError, match="token account") as exc:
            adapter.prepare_task(contract)
        assert exc.value.code is ErrorCode.MISSING_TOKEN_ACCOUNT


class TestSubmit:
    def test_funds_task_and_debits(self, adapter: MockGibworkAdapter, contract: TaskContract, clock: FakeClock) -> None:
        prepared = adapter.prepare_task(contract)
        submitted = adapter.submit_task(prepared.confirmation_id)
        assert submitted.status == "fulfilled"
        assert submitted.task_id == prepared.task_id
        assert submitted.intent_id == prepared.intent_id
        assert submitted.confirmation_id == prepared.confirmation_id
        assert submitted.signature.startswith("mock")
        assert submitted.submitted_at == clock.now
        assert adapter.balance == Decimal("95.00")

        task = adapter.get_task(prepared.task_id)
        assert task is not None
        assert task.title == contract.title
        assert task.content == contract.description
        assert task.tags == ["errand", "photo"]
        assert task.is_open is True
        assert task.status == "CREATED"
        assert task.funding_amount == "5.00"
        assert task.min_submission_amount == "5.00"
        assert adapter.list_tasks() == [task]

    def test_confirmation_is_one_shot(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        prepared = adapter.prepare_task(contract)
        adapter.submit_task(prepared.confirmation_id)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID
        assert adapter.balance == Decimal("95.00")

    def test_unknown_confirmation(self, adapter: MockGibworkAdapter) -> None:
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task("00000000-0000-4000-8000-000000000000")
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID

    def test_expires_after_ttl(self, adapter: MockGibworkAdapter, contract: TaskContract, clock: FakeClock) -> None:
        prepared = adapter.prepare_task(contract)
        clock.advance(minutes=5)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_EXPIRED

    def test_valid_just_before_expiry(self, adapter: MockGibworkAdapter, contract: TaskContract, clock: FakeClock) -> None:
        prepared = adapter.prepare_task(contract)
        clock.advance(minutes=4, seconds=59)
        adapter.submit_task(prepared.confirmation_id)

    def test_insufficient_funds(self, contract: TaskContract, clock: FakeClock) -> None:
        adapter = MockGibworkAdapter(balance=Decimal("2.00"), clock=clock)
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_task(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.INSUFFICIENT_FUNDS
        assert adapter.balance == Decimal("2.00")
        assert adapter.list_tasks() == []


class TestPersistence:
    def test_state_survives_reload(self, tmp_path: Path, contract: TaskContract, clock: FakeClock) -> None:
        path = tmp_path / "state.json"
        first = MockGibworkAdapter(state_path=path, clock=clock)
        prepared = first.prepare_task(contract)

        second = MockGibworkAdapter(state_path=path, clock=clock)
        submitted = second.submit_task(prepared.confirmation_id)

        third = MockGibworkAdapter(state_path=path, clock=clock)
        assert third.balance == Decimal("95.00")
        assert [t.id for t in third.list_tasks()] == [submitted.task_id]
        with pytest.raises(AdapterError):
            third.submit_task(prepared.confirmation_id)

    def test_constructor_overrides_persist(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        MockGibworkAdapter(state_path=path, balance=Decimal("7.50"))
        assert MockGibworkAdapter(state_path=path).balance == Decimal("7.50")


class TestInspection:
    def test_wallet_status(self, adapter: MockGibworkAdapter) -> None:
        status = adapter.wallet_status()
        assert status.adapter == "mock"
        assert status.environment == "mock"
        assert status.wallet_address == adapter.wallet_address
        assert status.writes_enabled is True

    def test_seed_list_get_submissions(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        prepared = adapter.prepare_task(contract)
        submitted = adapter.submit_task(prepared.confirmation_id)
        sub = adapter.seed_submission(submitted.task_id, content="done", submitter="alice")
        approved = adapter.seed_submission(submitted.task_id, content="also", status="approved")
        assert [s.id for s in adapter.list_submissions(submitted.task_id)] == [sub.id, approved.id]
        assert adapter.list_submissions(submitted.task_id, status="pending") == [sub]
        assert adapter.get_submission(submitted.task_id, sub.id) == sub
        assert adapter.get_submission(submitted.task_id, "nope") is None
        assert adapter.get_task(submitted.task_id).total_submissions == 2

    def test_seed_requires_task(self, adapter: MockGibworkAdapter) -> None:
        with pytest.raises(AdapterError) as exc:
            adapter.seed_submission("missing", content="x")
        assert exc.value.code is ErrorCode.NOT_FOUND


class TestRefund:
    def test_refund_round_trip(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        prepared = adapter.prepare_task(contract)
        submitted = adapter.submit_task(prepared.confirmation_id)
        assert adapter.balance == Decimal("95.00")

        refund = adapter.prepare_refund(submitted.task_id)
        assert refund.task_id == submitted.task_id
        assert refund.refund_amount == "5.00"
        assert refund.token is not None and refund.token.symbol == "USDC"
        assert adapter.balance == Decimal("95.00")  # prepare moves nothing

        result = adapter.submit_refund(refund.confirmation_id)
        assert result.status == "fulfilled"
        assert adapter.balance == Decimal("100.00")
        task = adapter.get_task(submitted.task_id)
        assert task is not None and task.is_open is False and task.status == "CLOSED"

    def test_refund_unknown_task(self, adapter: MockGibworkAdapter) -> None:
        with pytest.raises(AdapterError) as exc:
            adapter.prepare_refund("missing")
        assert exc.value.code is ErrorCode.NOT_FOUND

    def test_refund_closed_task_rejected(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        prepared = adapter.prepare_task(contract)
        submitted = adapter.submit_task(prepared.confirmation_id)
        refund = adapter.prepare_refund(submitted.task_id)
        adapter.submit_refund(refund.confirmation_id)
        with pytest.raises(AdapterError):
            adapter.prepare_refund(submitted.task_id)

    def test_confirmation_bound_to_operation(self, adapter: MockGibworkAdapter, contract: TaskContract) -> None:
        prepared = adapter.prepare_task(contract)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_refund(prepared.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_INVALID
        # The mismatched attempt did not consume it.
        adapter.submit_task(prepared.confirmation_id)

    def test_refund_confirmation_expires(self, adapter: MockGibworkAdapter, contract: TaskContract, clock: FakeClock) -> None:
        prepared = adapter.prepare_task(contract)
        submitted = adapter.submit_task(prepared.confirmation_id)
        refund = adapter.prepare_refund(submitted.task_id)
        clock.advance(minutes=6)
        with pytest.raises(AdapterError) as exc:
            adapter.submit_refund(refund.confirmation_id)
        assert exc.value.code is ErrorCode.CONFIRMATION_EXPIRED
