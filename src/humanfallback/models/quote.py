"""Shapes exchanged with a Gibwork adapter (mock or real)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TokenInfo(BaseModel):
    mint_address: str
    symbol: str
    decimals: int


class PlatformFee(BaseModel):
    percent: float
    amount: str


class PaymentQuote(BaseModel):
    token: TokenInfo
    funding_amount: str
    platform_fee: PlatformFee
    total_debit: str


class PrepareResult(BaseModel):
    """Returned by prepare_task. Nothing has been signed or funded yet."""

    confirmation_id: str
    intent_id: str
    task_id: str
    wallet_address: str
    environment: str
    created_at: datetime
    expires_at: datetime
    payment_quote: PaymentQuote
    next_action: str


class SubmitResult(BaseModel):
    """Returned by submit_task after the funding transaction is accepted."""

    task_id: str
    intent_id: str
    confirmation_id: str
    signature: str
    status: str
    submitted_at: datetime


class RemoteTaskStatus(StrEnum):
    CREATED = "CREATED"
    CLOSED = "CLOSED"


class RemoteTask(BaseModel):
    """A bounty as seen from the adapter's side."""

    id: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    status: RemoteTaskStatus
    is_open: bool
    token: TokenInfo
    funding_amount: str
    min_submission_amount: str
    deadline: datetime | None = None
    created_at: datetime
