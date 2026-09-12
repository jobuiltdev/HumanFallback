"""The Task Contract: the unit of work handed from an agent to a human."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .acceptance import AcceptanceCriterion
from .classification import ClassificationResult
from .evidence import EvidenceRequirement
from .quote import PaymentQuote

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
MAX_TAGS = 3
MAX_TAG_LEN = 32


def utcnow() -> datetime:
    return datetime.now(UTC)


class ContractStatus(StrEnum):
    DRAFT = "draft"  # built but not cleared for delegation
    READY = "ready"  # cleared for delegation
    SUBMIT_UNCERTAIN = "submit_uncertain"  # a money-moving submit has an unknown outcome
    DELEGATED = "delegated"  # bounty created and funded
    SUBMITTED = "submitted"  # at least one submission awaiting review
    APPROVED = "approved"
    REJECTED = "rejected"
    CLOSED = "closed"


_S = ContractStatus

TRANSITIONS: dict[ContractStatus, frozenset[ContractStatus]] = {
    _S.DRAFT: frozenset({_S.READY, _S.CLOSED}),
    _S.READY: frozenset({_S.DELEGATED, _S.DRAFT, _S.CLOSED, _S.SUBMIT_UNCERTAIN}),
    # Leaves only through reconciliation or an explicit manual resolution.
    _S.SUBMIT_UNCERTAIN: frozenset({_S.READY, _S.DELEGATED, _S.SUBMITTED, _S.CLOSED}),
    _S.DELEGATED: frozenset({_S.SUBMITTED, _S.CLOSED, _S.SUBMIT_UNCERTAIN}),
    _S.SUBMITTED: frozenset({_S.APPROVED, _S.REJECTED, _S.CLOSED, _S.SUBMIT_UNCERTAIN}),
    _S.APPROVED: frozenset({_S.CLOSED}),
    _S.REJECTED: frozenset({_S.SUBMITTED, _S.CLOSED}),
    _S.CLOSED: frozenset(),
}

# Statuses from which a money-moving submit may be started.
DELEGATABLE = frozenset({_S.READY})
REFUNDABLE = frozenset({_S.DELEGATED, _S.SUBMITTED})


class InvalidTransition(ValueError):
    pass


class Reward(BaseModel):
    """Total funding and per-submission floor, as two-decimal strings."""

    amount: str
    min_submission_amount: str | None = None
    mint_address: str = USDC_MINT
    symbol: str = "USDC"
    decimals: int = 6

    @field_validator("amount", "min_submission_amount")
    @classmethod
    def _two_decimals(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not AMOUNT_RE.match(value):
            raise ValueError(f"amount must have exactly two decimals, got {value!r}")
        if Decimal(value) <= 0:
            raise ValueError("amount must be positive")
        return value

    @model_validator(mode="after")
    def _default_min(self) -> Reward:
        if self.min_submission_amount is None:
            self.min_submission_amount = self.amount
        elif Decimal(self.min_submission_amount) > Decimal(self.amount):
            raise ValueError("min_submission_amount cannot exceed amount")
        return self

    @property
    def amount_decimal(self) -> Decimal:
        return Decimal(self.amount)


class DelegationRecord(BaseModel):
    """Identifiers returned by the adapter once a bounty is funded."""

    adapter: str
    task_id: str
    intent_id: str | None
    confirmation_id: str
    signature: str
    quote: PaymentQuote
    delegated_at: datetime


MoneyOperation = Literal["task_create", "task_refund"]


class AttemptOutcome(StrEnum):
    PREPARED = "prepared"  # quote obtained, nothing dispatched
    SUBMITTED = "submitted"  # backend confirmed the submit
    FAILED = "failed"  # backend rejected before dispatch; safe to retry
    AMBIGUOUS = "ambiguous"  # dispatched, outcome unknown; never retry
    RECONCILED_CONFIRMED = "reconciled_confirmed"
    RECONCILED_NOT_FOUND = "reconciled_not_found"


class DelegationAttempt(BaseModel):
    """Audit record for one prepare/submit cycle of a money-moving operation."""

    operation: MoneyOperation
    adapter: str
    environment: str
    wallet_address: str
    origin_status: ContractStatus
    task_id: str
    confirmation_id: str
    intent_id: str | None = None
    quote: PaymentQuote | None = None
    prepared_at: datetime
    outcome: AttemptOutcome = AttemptOutcome.PREPARED
    error: str | None = None
    resolved_at: datetime | None = None


class RefundRecord(BaseModel):
    adapter: str
    task_id: str
    confirmation_id: str
    signature: str
    quote: dict[str, Any] = Field(default_factory=dict)
    refunded_at: datetime


class RemoteSnapshot(BaseModel):
    """Last observed state of the bounty on the backend."""

    status: str
    is_open: bool
    total_submissions: int | None = None
    synced_at: datetime


class TaskContract(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=10000)
    source_request: str = Field(min_length=1)
    classification: ClassificationResult
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    evidence_requirements: list[EvidenceRequirement] = Field(min_length=1)
    reward: Reward
    deadline: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    status: ContractStatus = ContractStatus.DRAFT
    delegation: DelegationRecord | None = None
    refund: RefundRecord | None = None
    remote: RemoteSnapshot | None = None
    attempts: list[DelegationAttempt] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("tags")
    @classmethod
    def _tags(cls, tags: list[str]) -> list[str]:
        seen: set[str] = set()
        for tag in tags:
            if len(tag) > MAX_TAG_LEN or not TAG_RE.match(tag):
                raise ValueError(f"invalid tag {tag!r}")
            key = tag.lower()
            if key in seen:
                raise ValueError(f"duplicate tag {tag!r}")
            seen.add(key)
        return tags

    @model_validator(mode="after")
    def _check_references(self) -> TaskContract:
        ev_ids = [e.id for e in self.evidence_requirements]
        if len(ev_ids) != len(set(ev_ids)):
            raise ValueError("evidence requirement ids must be unique")
        ac_ids = [c.id for c in self.acceptance_criteria]
        if len(ac_ids) != len(set(ac_ids)):
            raise ValueError("acceptance criterion ids must be unique")
        known = set(ev_ids)
        for criterion in self.acceptance_criteria:
            missing = [e for e in criterion.evidence_ids if e not in known]
            if missing:
                raise ValueError(
                    f"criterion {criterion.id} references unknown evidence {missing}"
                )
        return self

    @property
    def uncertain_attempt(self) -> DelegationAttempt | None:
        """The attempt blocking further submits, if any."""
        for attempt in reversed(self.attempts):
            if attempt.outcome is AttemptOutcome.AMBIGUOUS:
                return attempt
        return None

    def can_transition_to(self, new_status: ContractStatus) -> bool:
        return new_status in TRANSITIONS[self.status]

    def transition_to(self, new_status: ContractStatus) -> None:
        if not self.can_transition_to(new_status):
            raise InvalidTransition(f"cannot move from {self.status} to {new_status}")
        self.status = new_status
        self.updated_at = utcnow()
