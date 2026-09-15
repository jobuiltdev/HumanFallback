"""Turn a request plus its classification into a Task Contract.

Each category carries a template of default acceptance criteria and the
evidence that proves them. Templates are intentionally conservative: they
never ask a worker to upload identity documents or credentials. A caller
who knows exactly what to check can supply a ContractSpec instead, which
replaces the template outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from humanfallback.models import (
    AcceptanceCriterion,
    CheckType,
    ClassificationResult,
    ContractStatus,
    EvidenceKind,
    EvidenceRequirement,
    Reward,
    TaskCategory,
    TaskContract,
)

MAX_TITLE = 120


class AgentCapableRequest(ValueError):
    """Raised when asked to build a contract for work an agent can do itself."""


@dataclass(frozen=True)
class EvidenceSpec:
    kind: EvidenceKind
    description: str
    constraints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CriterionSpec:
    statement: str
    evidence: tuple[int, ...]  # indexes into the template's evidence list
    check_type: CheckType = CheckType.MANUAL


@dataclass(frozen=True)
class Template:
    evidence: tuple[EvidenceSpec, ...]
    criteria: tuple[CriterionSpec, ...]


class ContractSpec(BaseModel):
    """Explicit evidence requirements and acceptance criteria, used verbatim
    in place of the category template. Ids must be unique and every
    criterion may only point at evidence declared here."""

    evidence_requirements: list[EvidenceRequirement] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_references(self) -> ContractSpec:
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
                raise ValueError(f"criterion {criterion.id} references unknown evidence {missing}")
            if criterion.check_type in (CheckType.EXACT_MATCH, CheckType.PATTERN) and not criterion.expected:
                raise ValueError(f"criterion {criterion.id} is {criterion.check_type.value} but has no expected value")
        return self


_PHOTO = EvidenceSpec(
    EvidenceKind.PHOTO,
    "Photo showing the completed action with visible location or time context",
)
_URL = EvidenceSpec(EvidenceKind.URL, "Public link to the resulting post or page")
_SCREENSHOT = EvidenceSpec(EvidenceKind.SCREENSHOT, "Screenshot of the completed state")
_RATIONALE = EvidenceSpec(
    EvidenceKind.TEXT,
    "Written rationale for the decision",
    {"min_words": "50"},
)
_SIGNED_DOC = EvidenceSpec(EvidenceKind.FILE, "The fully signed document as a PDF")
_DATASET = EvidenceSpec(
    EvidenceKind.FILE,
    "Collected data as CSV or JSON covering every requested field",
)
_CONVERSATION = EvidenceSpec(
    EvidenceKind.TEXT,
    "Summary of the conversation including who was contacted and the outcome",
    {"min_words": "30"},
)
_DELIVERABLE = EvidenceSpec(EvidenceKind.TEXT, "The deliverable, or a link to it")

TEMPLATES: dict[TaskCategory, Template] = {
    TaskCategory.PHYSICAL_ACTION: Template(
        evidence=(_PHOTO,),
        criteria=(
            CriterionSpec("Action was performed at the specified place", (0,)),
            CriterionSpec("Photo evidence is present and unambiguous", (0,), CheckType.EVIDENCE_PRESENT),
        ),
    ),
    TaskCategory.IDENTITY_VERIFICATION: Template(
        evidence=(
            EvidenceSpec(
                EvidenceKind.SCREENSHOT,
                "Screenshot of the verification-complete confirmation screen; "
                "do not include identity documents",
            ),
        ),
        criteria=(
            CriterionSpec("Verification status shows as complete", (0,)),
        ),
    ),
    TaskCategory.SUBJECTIVE_JUDGMENT: Template(
        evidence=(_RATIONALE,),
        criteria=(
            CriterionSpec("A single clear choice or rating is stated", (0,)),
            CriterionSpec("Rationale explains the choice", (0,), CheckType.EVIDENCE_PRESENT),
        ),
    ),
    TaskCategory.ACCOUNT_ACCESS: Template(
        evidence=(_URL, _SCREENSHOT),
        criteria=(
            CriterionSpec("Action was performed from the worker's own account", (0, 1)),
            CriterionSpec("Result is publicly reachable at the supplied link", (0,), CheckType.EVIDENCE_PRESENT),
        ),
    ),
    TaskCategory.LEGAL_SIGNATURE: Template(
        evidence=(_SIGNED_DOC,),
        criteria=(
            CriterionSpec("Every required signature field is completed", (0,)),
            CriterionSpec("Signed document is attached", (0,), CheckType.EVIDENCE_PRESENT),
        ),
    ),
    TaskCategory.OFFLINE_DATA_COLLECTION: Template(
        evidence=(_DATASET, _PHOTO),
        criteria=(
            CriterionSpec("Data covers every requested field", (0,)),
            CriterionSpec("Photo corroborates the data was gathered on site", (1,)),
        ),
    ),
    TaskCategory.HUMAN_INTERACTION: Template(
        evidence=(_CONVERSATION,),
        criteria=(
            CriterionSpec("Outcome of the conversation is recorded", (0,)),
        ),
    ),
    TaskCategory.AGENT_CAPABLE: Template(
        evidence=(_DELIVERABLE,),
        criteria=(
            CriterionSpec("Deliverable matches the request", (0,)),
        ),
    ),
}

_UNIVERSAL_CRITERION = "Submission fulfils the request as described"


def _from_template(template: Template) -> tuple[list[EvidenceRequirement], list[AcceptanceCriterion]]:
    evidence = [
        EvidenceRequirement(
            id=f"ev-{i + 1}",
            kind=spec.kind,
            description=spec.description,
            constraints=dict(spec.constraints),
        )
        for i, spec in enumerate(template.evidence)
    ]
    all_ev_ids = [e.id for e in evidence]
    criteria = [
        AcceptanceCriterion(id="ac-1", statement=_UNIVERSAL_CRITERION, evidence_ids=all_ev_ids)
    ]
    for i, spec in enumerate(template.criteria):
        criteria.append(
            AcceptanceCriterion(
                id=f"ac-{i + 2}",
                statement=spec.statement,
                check_type=spec.check_type,
                evidence_ids=[evidence[j].id for j in spec.evidence],
            )
        )
    return evidence, criteria


def derive_title(request: str) -> str:
    """First sentence of the request, capitalised and capped at 120 chars."""
    segments = (seg.strip() for seg in re.split(r"[.!?\n]", request))
    first = next((seg for seg in segments if seg), request.strip())
    first = first[0].upper() + first[1:]
    if len(first) > MAX_TITLE:
        first = first[: MAX_TITLE - 3].rstrip() + "..."
    return first


def build_contract(
    request: str,
    classification: ClassificationResult,
    reward: Reward,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    deadline: datetime | None = None,
    allow_agent_capable: bool = False,
    spec: ContractSpec | None = None,
) -> TaskContract:
    if not classification.human_required and not allow_agent_capable:
        raise AgentCapableRequest(
            "request was classified as agent-capable; pass allow_agent_capable=True to override"
        )

    if spec is not None:
        evidence = [e.model_copy(deep=True) for e in spec.evidence_requirements]
        criteria = [c.model_copy(deep=True) for c in spec.acceptance_criteria]
    else:
        evidence, criteria = _from_template(TEMPLATES[classification.category])

    status = ContractStatus.READY if classification.human_required else ContractStatus.DRAFT
    return TaskContract(
        title=title or derive_title(request),
        description=request.strip(),
        source_request=request,
        classification=classification,
        acceptance_criteria=criteria,
        evidence_requirements=evidence,
        reward=reward,
        deadline=deadline,
        tags=tags or [],
        status=status,
    )
