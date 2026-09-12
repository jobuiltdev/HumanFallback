"""Output of the human-required task classifier."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskCategory(StrEnum):
    """Why a task needs a human, or AGENT_CAPABLE if it does not."""

    PHYSICAL_ACTION = "physical_action"
    IDENTITY_VERIFICATION = "identity_verification"
    SUBJECTIVE_JUDGMENT = "subjective_judgment"
    ACCOUNT_ACCESS = "account_access"
    LEGAL_SIGNATURE = "legal_signature"
    OFFLINE_DATA_COLLECTION = "offline_data_collection"
    HUMAN_INTERACTION = "human_interaction"
    AGENT_CAPABLE = "agent_capable"


HUMAN_CATEGORIES: frozenset[TaskCategory] = frozenset(
    c for c in TaskCategory if c is not TaskCategory.AGENT_CAPABLE
)


class Signal(BaseModel):
    """One matched rule and the text that triggered it."""

    category: TaskCategory
    label: str
    weight: float
    matched_text: str


class ClassificationResult(BaseModel):
    human_required: bool
    category: TaskCategory
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    classifier: str
