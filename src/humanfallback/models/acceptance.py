"""Acceptance criteria: what must be true for a submission to be accepted."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class CheckType(StrEnum):
    MANUAL = "manual"  # a reviewer decides
    EVIDENCE_PRESENT = "evidence_present"  # linked evidence was supplied
    EXACT_MATCH = "exact_match"  # evidence text equals `expected`
    PATTERN = "pattern"  # evidence text matches regex in `expected`


class AcceptanceCriterion(BaseModel):
    id: str = Field(pattern=r"^ac-\d+$")
    statement: str = Field(min_length=1, max_length=500)
    check_type: CheckType = CheckType.MANUAL
    expected: str | None = None
    required: bool = True
    evidence_ids: list[str] = Field(default_factory=list)
