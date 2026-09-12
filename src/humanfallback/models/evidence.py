"""Evidence requirements: what artifacts prove a criterion was met."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class EvidenceKind(StrEnum):
    URL = "url"
    SCREENSHOT = "screenshot"
    PHOTO = "photo"
    FILE = "file"
    TEXT = "text"
    TRANSACTION = "transaction"


class EvidenceRequirement(BaseModel):
    id: str = Field(pattern=r"^ev-\d+$")
    kind: EvidenceKind
    description: str = Field(min_length=1, max_length=500)
    required: bool = True
    # Free-form constraints the evaluator can act on later, e.g.
    # {"url_pattern": "^https://x\.com/"} or {"min_words": "50"}.
    constraints: dict[str, str] = Field(default_factory=dict)
