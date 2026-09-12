"""In-process stand-in for the Gibwork task API.

Mirrors the constraints of the real staging service so the real adapter
can replace this one without changing callers:

* funding amount between 1.00 and 100000.00, two-decimal strings
* the funding wallet must hold a token account for the mint
* prepare returns confirmation, intent and task ids plus a quote
* confirmations expire after five minutes and can be consumed once
* platform fee is zero

State can be persisted to a JSON file so CLI invocations share it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from humanfallback.models import (
    USDC_MINT,
    PaymentQuote,
    PlatformFee,
    PrepareResult,
    RemoteTask,
    SubmitResult,
    TaskContract,
    TokenInfo,
)
from humanfallback.models.quote import RemoteTaskStatus

from .base import AdapterError

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


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(value: Decimal) -> str:
    return str(value.quantize(_CENTS))


class _PendingConfirmation(BaseModel):
    confirmation_id: str
    intent_id: str
    task_id: str
    contract_id: str
    title: str
    content: str
    tags: list[str]
    mint_address: str
    funding_amount: str
    min_submission_amount: str
    deadline: datetime | None
    created_at: datetime
    expires_at: datetime
    consumed: bool = False


class _State(BaseModel):
    balance: str = _fmt(DEFAULT_BALANCE)
    has_token_account: bool = True
    pending: dict[str, _PendingConfirmation] = Field(default_factory=dict)
    tasks: dict[str, RemoteTask] = Field(default_factory=dict)


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
        self._clock = clock
        self._state_path = state_path
        self._state = self._load()
        if balance is not None:
            self._state.balance = _fmt(balance)
        if has_token_account is not None:
            self._state.has_token_account = has_token_account
        self._save()

    # -- public API -----------------------------------------------------

    @property
    def balance(self) -> Decimal:
        return Decimal(self._state.balance)

    def prepare_task(self, contract: TaskContract) -> PrepareResult:
        reward = contract.reward
        token = SUPPORTED_TOKENS.get(reward.mint_address)
        if token is None:
            raise AdapterError("UNSUPPORTED_MINT", f"mint {reward.mint_address} is not supported")
        amount = reward.amount_decimal
        if not MIN_FUNDING <= amount <= MAX_FUNDING:
            raise AdapterError(
                "API_ERROR",
                f"payment.amount must be between {MIN_FUNDING} and {MAX_FUNDING} inclusive",
            )
        if not self._state.has_token_account:
            raise AdapterError(
                "API_ERROR",
                f"The funding wallet does not have an initialized {token.symbol} token account.",
            )

        now = self._clock()
        pending = _PendingConfirmation(
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
        )

    def submit_task(self, confirmation_id: str) -> SubmitResult:
        pending = self._state.pending.get(confirmation_id)
        if pending is None:
            raise AdapterError("UNKNOWN_CONFIRMATION", "no prepared transaction with that id")
        if pending.consumed:
            raise AdapterError("ALREADY_CONSUMED", "confirmation was already submitted")
        now = self._clock()
        if now >= pending.expires_at:
            raise AdapterError("EXPIRED", "confirmation has expired; prepare again")
        amount = Decimal(pending.funding_amount)
        if self.balance < amount:
            raise AdapterError(
                "INSUFFICIENT_FUNDS",
                f"wallet holds {self.balance} but {amount} is required",
            )

        pending.consumed = True
        self._state.balance = _fmt(self.balance - amount)
        token = SUPPORTED_TOKENS[pending.mint_address]
        self._state.tasks[pending.task_id] = RemoteTask(
            id=pending.task_id,
            title=pending.title,
            content=pending.content,
            tags=pending.tags,
            status=RemoteTaskStatus.CREATED,
            is_open=True,
            token=token,
            funding_amount=pending.funding_amount,
            min_submission_amount=pending.min_submission_amount,
            deadline=pending.deadline,
            created_at=now,
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

    def list_tasks(self) -> list[RemoteTask]:
        return sorted(self._state.tasks.values(), key=lambda t: t.created_at)

    def get_task(self, task_id: str) -> RemoteTask | None:
        return self._state.tasks.get(task_id)

    # -- internals ------------------------------------------------------

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
