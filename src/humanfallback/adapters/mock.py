"""In-process stand-in for the Gibwork task API.

Mirrors the constraints of the real staging service so the real adapter
can replace this one without changing callers:

* funding amount between 1.00 and 100000.00, two-decimal strings
* the funding wallet must hold a token account for the mint
* prepare returns confirmation, intent and task ids plus a quote
* confirmations expire after five minutes, are bound to one operation
  type, and can be consumed once
* platform fee is zero

State can be persisted to a JSON file so CLI invocations share it.
Submissions can be seeded for testing review flows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from humanfallback.models import (
    USDC_MINT,
    PaymentQuote,
    PlatformFee,
    PrepareResult,
    RefundPrepareResult,
    RemoteSubmission,
    RemoteTask,
    RemoteTaskStatus,
    SubmitResult,
    TaskContract,
    TokenInfo,
    WalletStatus,
)

from .errors import AdapterError, ErrorCode

NAME = "mock"
MIN_FUNDING = Decimal("1.00")
MAX_FUNDING = Decimal("100000.00")
CONFIRMATION_TTL = timedelta(minutes=5)
DEFAULT_WALLET = "MockWa11etAddress1111111111111111111111111"
DEFAULT_BALANCE = Decimal("100.00")
_CENTS = Decimal("0.01")

SUPPORTED_TOKENS: dict[str, TokenInfo] = {
    USDC_MINT: TokenInfo(mint_address=USDC_MINT, symbol="USDC", decimals=6),
}

Clock = Callable[[], datetime]
PendingKind = Literal["task_create", "task_refund"]


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(value: Decimal) -> str:
    return str(value.quantize(_CENTS))


class _PendingConfirmation(BaseModel):
    kind: PendingKind
    confirmation_id: str
    intent_id: str
    task_id: str
    contract_id: str | None = None
    title: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    mint_address: str = USDC_MINT
    funding_amount: str
    min_submission_amount: str
    deadline: datetime | None = None
    created_at: datetime
    expires_at: datetime
    consumed: bool = False


class _State(BaseModel):
    balance: str = _fmt(DEFAULT_BALANCE)
    has_token_account: bool = True
    pending: dict[str, _PendingConfirmation] = Field(default_factory=dict)
    tasks: dict[str, RemoteTask] = Field(default_factory=dict)
    submissions: dict[str, list[RemoteSubmission]] = Field(default_factory=dict)


class MockGibworkAdapter:
    name = NAME

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        wallet_address: str = DEFAULT_WALLET,
        environment: str = "mock",
        balance: Decimal | None = None,
        has_token_account: bool | None = None,
        clock: Clock = _now,
    ) -> None:
        self.wallet_address = wallet_address
        self.environment = environment
        self.writes = True
        self._clock = clock
        self._state_path = state_path
        self._state = self._load()
        if balance is not None:
            self._state.balance = _fmt(balance)
        if has_token_account is not None:
            self._state.has_token_account = has_token_account
        self._save()

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> WalletStatus:
        return self.wallet_status()

    def close(self) -> None:
        return None

    def __enter__(self) -> MockGibworkAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- inspection ---------------------------------------------------------

    @property
    def balance(self) -> Decimal:
        return Decimal(self._state.balance)

    def wallet_status(self) -> WalletStatus:
        return WalletStatus(
            adapter=self.name,
            profile="mock",
            environment=self.environment,
            wallet_address=self.wallet_address,
            writes_enabled=True,
        )

    def list_tasks(self) -> list[RemoteTask]:
        return sorted(self._state.tasks.values(), key=lambda t: t.created_at)

    def get_task(self, task_id: str) -> RemoteTask | None:
        return self._state.tasks.get(task_id)

    def list_submissions(
        self, task_id: str, *, status: str | None = None
    ) -> list[RemoteSubmission]:
        items = self._state.submissions.get(task_id, [])
        if status:
            items = [s for s in items if s.status == status]
        return list(items)

    def get_submission(self, task_id: str, submission_id: str) -> RemoteSubmission | None:
        for sub in self._state.submissions.get(task_id, []):
            if sub.id == submission_id:
                return sub
        return None

    def seed_submission(
        self,
        task_id: str,
        *,
        content: str,
        submitter: str = "worker",
        status: str = "pending",
        media: list[str] | None = None,
    ) -> RemoteSubmission:
        """Test helper: add a submission to an existing mock task."""
        task = self._state.tasks.get(task_id)
        if task is None:
            raise AdapterError(ErrorCode.NOT_FOUND, f"no task {task_id}")
        sub = RemoteSubmission(
            id=str(uuid.uuid4()),
            task_id=task_id,
            status=status,
            content=content,
            submitter=submitter,
            media=list(media or []),
            created_at=self._clock(),
        )
        self._state.submissions.setdefault(task_id, []).append(sub)
        task.total_submissions = (task.total_submissions or 0) + 1
        self._save()
        return sub

    # -- bounty creation ----------------------------------------------------

    def prepare_task(self, contract: TaskContract) -> PrepareResult:
        reward = contract.reward
        token = SUPPORTED_TOKENS.get(reward.mint_address)
        if token is None:
            raise AdapterError(
                ErrorCode.UNSUPPORTED_MINT, f"mint {reward.mint_address} is not supported"
            )
        amount = reward.amount_decimal
        if not MIN_FUNDING <= amount <= MAX_FUNDING:
            raise AdapterError(
                ErrorCode.AMOUNT_OUT_OF_RANGE,
                f"payment.amount must be between {MIN_FUNDING} and {MAX_FUNDING} inclusive",
            )
        if not self._state.has_token_account:
            raise AdapterError(
                ErrorCode.MISSING_TOKEN_ACCOUNT,
                f"The funding wallet does not have an initialized {token.symbol} token account.",
            )

        now = self._clock()
        pending = _PendingConfirmation(
            kind="task_create",
            confirmation_id=str(uuid.uuid4()),
            intent_id=str(uuid.uuid4()),
            task_id=str(uuid.uuid4()),
            contract_id=contract.id,
            title=contract.title,
            content=contract.description,
            tags=list(contract.tags),
            mint_address=reward.mint_address,
            funding_amount=reward.amount,
            min_submission_amount=reward.min_submission_amount or reward.amount,
            deadline=contract.deadline,
            created_at=now,
            expires_at=now + CONFIRMATION_TTL,
        )
        self._state.pending[pending.confirmation_id] = pending
        self._save()

        return PrepareResult(
            confirmation_id=pending.confirmation_id,
            intent_id=pending.intent_id,
            task_id=pending.task_id,
            wallet_address=self.wallet_address,
            environment=self.environment,
            created_at=pending.created_at,
            expires_at=pending.expires_at,
            payment_quote=self._quote(token, amount),
            next_action=(
                "Review the quote, then obtain explicit approval before calling "
                "submit_task with this confirmation id."
            ),
            last_valid_block_height=None,
        )

    def submit_task(self, confirmation_id: str) -> SubmitResult:
        pending = self._take(confirmation_id, "task_create")
        amount = Decimal(pending.funding_amount)
        if self.balance < amount:
            pending.consumed = False
            raise AdapterError(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"wallet holds {self.balance} but {amount} is required",
            )
        now = self._clock()
        self._state.balance = _fmt(self.balance - amount)
        token = SUPPORTED_TOKENS[pending.mint_address]
        self._state.tasks[pending.task_id] = RemoteTask(
            id=pending.task_id,
            title=pending.title,
            content=pending.content,
            tags=pending.tags,
            status=RemoteTaskStatus.CREATED.value,
            is_open=True,
            token=token,
            funding_amount=pending.funding_amount,
            min_submission_amount=pending.min_submission_amount,
            deadline=pending.deadline,
            created_at=now,
            total_submissions=0,
            can_refund=True,
        )
        self._save()
        return SubmitResult(
            task_id=pending.task_id,
            intent_id=pending.intent_id,
            confirmation_id=confirmation_id,
            signature="mock" + uuid.uuid4().hex,
            status="fulfilled",
            submitted_at=now,
        )

    # -- refund -------------------------------------------------------------

    def prepare_refund(self, task_id: str) -> RefundPrepareResult:
        task = self._state.tasks.get(task_id)
        if task is None:
            raise AdapterError(ErrorCode.NOT_FOUND, f"no task {task_id}")
        if not task.is_open:
            raise AdapterError(ErrorCode.API_ERROR, "task is not open; nothing to refund")
        now = self._clock()
        pending = _PendingConfirmation(
            kind="task_refund",
            confirmation_id=str(uuid.uuid4()),
            intent_id=str(uuid.uuid4()),
            task_id=task_id,
            mint_address=task.token.mint_address,
            funding_amount=task.funding_amount,
            min_submission_amount=task.min_submission_amount,
            created_at=now,
            expires_at=now + CONFIRMATION_TTL,
        )
        self._state.pending[pending.confirmation_id] = pending
        self._save()
        return RefundPrepareResult(
            confirmation_id=pending.confirmation_id,
            task_id=task_id,
            intent_id=pending.intent_id,
            wallet_address=self.wallet_address,
            environment=self.environment,
            created_at=now,
            expires_at=pending.expires_at,
            refund_amount=task.funding_amount,
            token=task.token,
            next_action="Review the refund, then obtain explicit approval before submit_refund.",
            raw={"refundAmount": task.funding_amount},
        )

    def submit_refund(self, confirmation_id: str) -> SubmitResult:
        pending = self._take(confirmation_id, "task_refund")
        task = self._state.tasks[pending.task_id]
        now = self._clock()
        self._state.balance = _fmt(self.balance + Decimal(pending.funding_amount))
        task.status = RemoteTaskStatus.CLOSED.value
        task.is_open = False
        task.can_refund = False
        self._save()
        return SubmitResult(
            task_id=pending.task_id,
            intent_id=pending.intent_id,
            confirmation_id=confirmation_id,
            signature="mockrefund" + uuid.uuid4().hex,
            status="fulfilled",
            submitted_at=now,
        )

    # -- internals ----------------------------------------------------------

    def _take(self, confirmation_id: str, kind: PendingKind) -> _PendingConfirmation:
        pending = self._state.pending.get(confirmation_id)
        if pending is None:
            raise AdapterError(
                ErrorCode.CONFIRMATION_INVALID,
                "The confirmation ID is unknown, expired, or has already been used.",
            )
        if pending.consumed:
            raise AdapterError(
                ErrorCode.CONFIRMATION_INVALID, "confirmation was already submitted"
            )
        if pending.kind != kind:
            raise AdapterError(
                ErrorCode.CONFIRMATION_INVALID,
                "The confirmation ID belongs to a different operation type.",
            )
        if self._clock() >= pending.expires_at:
            raise AdapterError(
                ErrorCode.CONFIRMATION_EXPIRED,
                "The prepared operation has expired. Prepare it again before submitting.",
            )
        pending.consumed = True
        return pending

    @staticmethod
    def _quote(token: TokenInfo, amount: Decimal) -> PaymentQuote:
        return PaymentQuote(
            token=token,
            funding_amount=_fmt(amount),
            platform_fee=PlatformFee(percent=0, amount="0.00"),
            total_debit=_fmt(amount),
        )

    def _load(self) -> _State:
        if self._state_path and self._state_path.exists():
            return _State.model_validate_json(self._state_path.read_text(encoding="utf-8"))
        return _State()

    def _save(self) -> None:
        if self._state_path:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state.model_dump(mode="json"), indent=2), encoding="utf-8"
            )
