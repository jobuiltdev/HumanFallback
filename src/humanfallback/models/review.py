"""Advisory review of a submission against its Task Contract.

Everything here is produced by deterministic rules. A review never
approves, rejects, or pays; it explains what could be verified, what could
not, and what a person still has to decide.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from .acceptance import CheckType
from .evidence import EvidenceKind

REVIEWER = "rules-v1"


class CheckKind(StrEnum):
    FACTUAL = "factual"  # decided by a rule from concrete evidence
    JUDGMENT = "judgment"  # only a person can decide


class FlagKind(StrEnum):
    FACTUAL = "factual"  # observed directly in the submission
    INFERRED = "inferred"  # a heuristic; needs a person to confirm


class CheckOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"  # judgment criterion whose evidence is present
    BLOCKED = "blocked"  # judgment criterion whose evidence is missing


class EvidenceOutcome(StrEnum):
    FOUND = "found"
    FOUND_UNVERIFIED_TYPE = "found_unverified_type"  # e.g. a bare media id: present, type unknown
    FOUND_CONSTRAINT_FAILED = "found_constraint_failed"
    MISSING = "missing"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Recommendation(StrEnum):
    STRONG = "strong"
    ACCEPTABLE = "acceptable"
    INCOMPLETE = "incomplete"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REJECT_CANDIDATE = "reject_candidate"


class ConstraintResult(BaseModel):
    name: str
    expected: str
    actual: str
    passed: bool


class CriterionResult(BaseModel):
    criterion_id: str
    statement: str
    check_type: CheckType
    required: bool
    kind: CheckKind
    outcome: CheckOutcome
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str


class EvidenceResult(BaseModel):
    evidence_id: str
    kind: EvidenceKind
    required: bool
    outcome: EvidenceOutcome
    matched: list[str] = Field(default_factory=list)
    constraint_results: list[ConstraintResult] = Field(default_factory=list)
    detail: str


class DeliverableResult(BaseModel):
    name: str
    outcome: Literal["pass", "fail"]
    kind: CheckKind = CheckKind.FACTUAL
    detail: str


class MissingRequirement(BaseModel):
    kind: Literal["evidence", "criterion", "deliverable"]
    id: str
    description: str
    required: bool


class Flag(BaseModel):
    code: str
    severity: Severity
    kind: FlagKind
    message: str
    evidence: list[str] = Field(default_factory=list)


class ScoreComponent(BaseModel):
    name: str
    points: int
    max_points: int
    reason: str


class SubmissionReview(BaseModel):
    submission_id: str
    contract_id: str
    submitter: str | None = None
    reviewed_at: datetime
    reviewer: str = REVIEWER
    advisory: bool = True
    acceptance_results: list[CriterionResult] = Field(default_factory=list)
    evidence_results: list[EvidenceResult] = Field(default_factory=list)
    deliverable_results: list[DeliverableResult] = Field(default_factory=list)
    missing_requirements: list[MissingRequirement] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    score: int = Field(ge=0, le=100)
    score_breakdown: list[ScoreComponent] = Field(default_factory=list)
    recommendation: Recommendation
    human_judgment_required: bool
    summary: str
    rank: int | None = None

    def flags_with(self, severity: Severity) -> list[Flag]:
        return [f for f in self.flags if f.severity is severity]

    def has_flag(self, code: str) -> bool:
        return any(f.code == code for f in self.flags)


class SubmissionComparison(BaseModel):
    contract_id: str
    ranked_reviews: list[SubmissionReview] = Field(default_factory=list)
    strongest_submission_id: str | None = None
    comparison_notes: list[str] = Field(default_factory=list)
    human_action_required: str
    advisory: bool = True
