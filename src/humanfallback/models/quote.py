"""Shapes exchanged with a Gibwork adapter (mock or real)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

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


class WalletStatus(BaseModel):
    """What the backend resolved for signing, never including key material."""

    adapter: str
    profile: str
    environment: str
    wallet_address: str
    writes_enabled: bool


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
    last_valid_block_height: int | None = None


class RefundPrepareResult(BaseModel):
    """Returned by prepare_refund. Fields the backend omitted are None; the
    untouched backend payload is kept in `raw` so it can be shown in full."""

    confirmation_id: str
    task_id: str
    intent_id: str | None = None
    wallet_address: str | None = None
    environment: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    refund_amount: str | None = None
    token: TokenInfo | None = None
    next_action: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SubmitResult(BaseModel):
    """Returned by a submit after the signed transaction is accepted."""

    task_id: str
    intent_id: str | None
    confirmation_id: str
    signature: str
    status: str
    submitted_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class RemoteTaskStatus(StrEnum):
    CREATED = "CREATED"
    CLOSED = "CLOSED"


class RemoteTask(BaseModel):
    """A bounty as seen from the adapter's side."""

    id: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    status: str
    is_open: bool
    token: TokenInfo
    funding_amount: str
    min_submission_amount: str
    deadline: datetime | None = None
    created_at: datetime
    total_submissions: int | None = None
    max_submissions: int | None = None
    can_refund: bool | None = None


class RemoteSubmissionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RemoteSubmission(BaseModel):
    """A worker's submission on a bounty."""

    id: str
    task_id: str
    status: str
    content: str = ""
    submitter: str | None = None
    media: list[str] = Field(default_factory=list)
    rating: int | None = None
    created_at: datetime | None = None
    comments: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
