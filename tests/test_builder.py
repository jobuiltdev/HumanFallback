from __future__ import annotations

from datetime import UTC, datetime

import pytest

from humanfallback.classifier import default_classifier
from pydantic import ValidationError

from humanfallback.contracts import AgentCapableRequest, ContractSpec, build_contract, derive_title
from humanfallback.contracts.builder import TEMPLATES
from humanfallback.models import (
    CheckType,
    ClassificationResult,
    ContractStatus,
    EvidenceKind,
    Reward,
    TaskCategory,
)


def _classified(category: TaskCategory, human: bool = True) -> ClassificationResult:
    return ClassificationResult(
        human_required=human,
        category=category,
        confidence=0.8,
        reasons=["r"],
        classifier="test",
    )


class TestDeriveTitle:
    def test_first_sentence(self) -> None:
        assert derive_title("go to the shop. Then come back.") == "Go to the shop"

    def test_stops_at_newline(self) -> None:
        assert derive_title("Pick up keys\nfrom the office") == "Pick up keys"

    def test_truncates_long_input(self) -> None:
        title = derive_title("x" * 300)
        assert len(title) == 120
        assert title.endswith("...")

    def test_handles_leading_punctuation(self) -> None:
        assert derive_title("...\nreal request") == "Real request"


class TestBuildContract:
    def test_physical_action_template(self) -> None:
        req = "Go to the store and take a photo of the shelf."
        c = build_contract(req, _classified(TaskCategory.PHYSICAL_ACTION), Reward(amount="2.00"))
        assert c.status is ContractStatus.READY
        assert c.title == "Go to the store and take a photo of the shelf"
        assert c.description == req
        assert [e.kind for e in c.evidence_requirements] == [EvidenceKind.PHOTO]
        assert c.acceptance_criteria[0].id == "ac-1"
        assert c.acceptance_criteria[0].evidence_ids == ["ev-1"]
        assert any(a.check_type is CheckType.EVIDENCE_PRESENT for a in c.acceptance_criteria)

    def test_account_access_links_two_evidence_items(self) -> None:
        c = build_contract("Tweet it", _classified(TaskCategory.ACCOUNT_ACCESS), Reward(amount="1.00"))
        kinds = {e.kind for e in c.evidence_requirements}
        assert kinds == {EvidenceKind.URL, EvidenceKind.SCREENSHOT}
        assert c.acceptance_criteria[1].evidence_ids == ["ev-1", "ev-2"]

    def test_identity_template_never_asks_for_documents(self) -> None:
        c = build_contract("Do KYC", _classified(TaskCategory.IDENTITY_VERIFICATION), Reward(amount="1.00"))
        assert all(e.kind is not EvidenceKind.FILE for e in c.evidence_requirements)
        assert "do not include identity documents" in c.evidence_requirements[0].description

    def test_subjective_rationale_has_word_constraint(self) -> None:
        c = build_contract("Which is better?", _classified(TaskCategory.SUBJECTIVE_JUDGMENT), Reward(amount="1.00"))
        assert c.evidence_requirements[0].constraints == {"min_words": "50"}

    @pytest.mark.parametrize("category", list(TEMPLATES))
    def test_every_template_produces_valid_contract(self, category: TaskCategory) -> None:
        c = build_contract(
            "some request",
            _classified(category, human=category is not TaskCategory.AGENT_CAPABLE),
            Reward(amount="1.00"),
            allow_agent_capable=True,
        )
        ev_ids = {e.id for e in c.evidence_requirements}
        for ac in c.acceptance_criteria:
            assert set(ac.evidence_ids) <= ev_ids
        assert c.acceptance_criteria[0].evidence_ids == sorted(ev_ids)

    def test_refuses_agent_capable_by_default(self) -> None:
        with pytest.raises(AgentCapableRequest):
            build_contract("Write tests", _classified(TaskCategory.AGENT_CAPABLE, human=False), Reward(amount="1.00"))

    def test_agent_capable_override_yields_draft(self) -> None:
        c = build_contract(
            "Write tests",
            _classified(TaskCategory.AGENT_CAPABLE, human=False),
            Reward(amount="1.00"),
            allow_agent_capable=True,
        )
        assert c.status is ContractStatus.DRAFT

    def test_passes_through_overrides(self) -> None:
        deadline = datetime(2026, 12, 1, tzinfo=UTC)
        c = build_contract(
            "Sign the lease",
            _classified(TaskCategory.LEGAL_SIGNATURE),
            Reward(amount="3.00", min_submission_amount="1.50"),
            title="Custom title",
            tags=["legal"],
            deadline=deadline,
        )
        assert c.title == "Custom title"
        assert c.tags == ["legal"]
        assert c.deadline == deadline
        assert c.reward.min_submission_amount == "1.50"

    def test_end_to_end_with_real_classifier(self) -> None:
        req = "Call the landlord and negotiate the renewal."
        c = build_contract(req, default_classifier().classify(req), Reward(amount="1.00"))
        assert c.classification.category is TaskCategory.HUMAN_INTERACTION
        assert c.source_request == req


def _spec(**overrides) -> dict:
    data = {
        "evidence_requirements": [
            {"id": "ev-1", "kind": "screenshot", "description": "Terminal screenshot"},
            {"id": "ev-2", "kind": "text", "description": "Output and feedback", "constraints": {"min_words": "60"}},
        ],
        "acceptance_criteria": [
            {"id": "ac-1", "statement": "Fulfils the request", "evidence_ids": ["ev-1", "ev-2"]},
            {"id": "ac-2", "statement": "Screenshot attached", "check_type": "evidence_present", "evidence_ids": ["ev-1"]},
            {"id": "ac-3", "statement": "Version shown", "check_type": "pattern", "expected": r"humanfallback\s+0\.4\.0", "evidence_ids": ["ev-2"]},
        ],
    }
    data.update(overrides)
    return data


class TestContractSpec:
    def test_spec_replaces_template(self) -> None:
        spec = ContractSpec.model_validate(_spec())
        contract = build_contract(
            "Verify the setup and give your human feedback",
            _classified(TaskCategory.SUBJECTIVE_JUDGMENT),
            Reward(amount="1.00"),
            spec=spec,
        )
        assert [e.id for e in contract.evidence_requirements] == ["ev-1", "ev-2"]
        assert contract.evidence_requirements[0].kind is EvidenceKind.SCREENSHOT
        assert contract.evidence_requirements[1].constraints == {"min_words": "60"}
        assert [c.check_type for c in contract.acceptance_criteria] == [
            CheckType.MANUAL, CheckType.EVIDENCE_PRESENT, CheckType.PATTERN
        ]
        assert contract.acceptance_criteria[2].expected == r"humanfallback\s+0\.4\.0"
        assert contract.status is ContractStatus.READY

    def test_spec_is_copied_not_shared(self) -> None:
        spec = ContractSpec.model_validate(_spec())
        contract = build_contract("Verify", _classified(TaskCategory.SUBJECTIVE_JUDGMENT), Reward(amount="1.00"), spec=spec)
        contract.evidence_requirements[1].constraints["min_words"] = "1"
        assert spec.evidence_requirements[1].constraints["min_words"] == "60"

    def test_spec_still_refuses_agent_capable(self) -> None:
        with pytest.raises(AgentCapableRequest):
            build_contract("Write it", _classified(TaskCategory.AGENT_CAPABLE, human=False),
                           Reward(amount="1.00"), spec=ContractSpec.model_validate(_spec()))

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"evidence_requirements": []}, "at least 1 item"),
            ({"acceptance_criteria": []}, "at least 1 item"),
            ({"acceptance_criteria": [{"id": "ac-1", "statement": "x", "evidence_ids": ["ev-9"]}]}, "unknown evidence"),
            ({"acceptance_criteria": [{"id": "ac-1", "statement": "x", "check_type": "pattern"}]}, "no expected value"),
            ({"acceptance_criteria": [{"id": "ac-1", "statement": "x"}, {"id": "ac-1", "statement": "y"}]}, "must be unique"),
            ({"evidence_requirements": [{"id": "ev-1", "kind": "hologram", "description": "x"}]}, "kind"),
        ],
    )
    def test_spec_validation(self, overrides: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            ContractSpec.model_validate(_spec(**overrides))
