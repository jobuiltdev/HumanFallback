from __future__ import annotations

import pytest
from pydantic import ValidationError

from humanfallback.models import (
    USDC_MINT,
    AcceptanceCriterion,
    ClassificationResult,
    ContractStatus,
    EvidenceKind,
    EvidenceRequirement,
    InvalidTransition,
    Reward,
    TaskCategory,
    TaskContract,
)


def _classification() -> ClassificationResult:
    return ClassificationResult(
        human_required=True,
        category=TaskCategory.PHYSICAL_ACTION,
        confidence=0.9,
        reasons=["test"],
        classifier="test",
    )


def _contract(**overrides) -> TaskContract:
    base = dict(
        title="Test",
        description="Do the thing.",
        source_request="Do the thing.",
        classification=_classification(),
        acceptance_criteria=[
            AcceptanceCriterion(id="ac-1", statement="Done", evidence_ids=["ev-1"])
        ],
        evidence_requirements=[
            EvidenceRequirement(id="ev-1", kind=EvidenceKind.PHOTO, description="Proof")
        ],
        reward=Reward(amount="1.00"),
    )
    base.update(overrides)
    return TaskContract(**base)


class TestReward:
    def test_defaults_to_usdc(self) -> None:
        r = Reward(amount="1.00")
        assert r.mint_address == USDC_MINT
        assert r.symbol == "USDC"
        assert r.decimals == 6

    def test_min_submission_defaults_to_amount(self) -> None:
        assert Reward(amount="10.00").min_submission_amount == "10.00"

    @pytest.mark.parametrize("bad", ["1", "1.0", "1.000", "abc", "0.00", "-1.00"])
    def test_rejects_malformed_amounts(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Reward(amount=bad)

    def test_min_cannot_exceed_amount(self) -> None:
        with pytest.raises(ValidationError):
            Reward(amount="1.00", min_submission_amount="2.00")


class TestTaskContract:
    def test_round_trips_through_json(self) -> None:
        c = _contract(tags=["a", "b"])
        restored = TaskContract.model_validate_json(c.model_dump_json())
        assert restored == c

    def test_generates_uuid_and_timestamps(self) -> None:
        c = _contract()
        assert len(c.id) == 36
        assert c.created_at.tzinfo is not None
        assert c.status is ContractStatus.DRAFT

    def test_rejects_unknown_evidence_reference(self) -> None:
        with pytest.raises(ValidationError, match="unknown evidence"):
            _contract(
                acceptance_criteria=[
                    AcceptanceCriterion(id="ac-1", statement="x", evidence_ids=["ev-9"])
                ]
            )

    def test_rejects_duplicate_ids(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            _contract(
                evidence_requirements=[
                    EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="a"),
                    EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="b"),
                ]
            )

    def test_rejects_too_many_tags(self) -> None:
        with pytest.raises(ValidationError):
            _contract(tags=["a", "b", "c", "d"])

    @pytest.mark.parametrize("bad", ["-x", "bad!tag", "a" * 33])
    def test_rejects_malformed_tags(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            _contract(tags=[bad])

    def test_rejects_case_insensitive_duplicate_tags(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            _contract(tags=["Design", "design"])

    def test_rejects_long_title(self) -> None:
        with pytest.raises(ValidationError):
            _contract(title="x" * 121)


class TestTransitions:
    def test_happy_path(self) -> None:
        c = _contract()
        for nxt in (
            ContractStatus.READY,
            ContractStatus.DELEGATED,
            ContractStatus.SUBMITTED,
            ContractStatus.APPROVED,
            ContractStatus.CLOSED,
        ):
            c.transition_to(nxt)
            assert c.status is nxt

    def test_updates_timestamp(self) -> None:
        c = _contract()
        before = c.updated_at
        c.transition_to(ContractStatus.READY)
        assert c.updated_at >= before

    def test_rejects_skipping_states(self) -> None:
        c = _contract()
        with pytest.raises(InvalidTransition):
            c.transition_to(ContractStatus.DELEGATED)

    def test_closed_is_terminal(self) -> None:
        c = _contract(status=ContractStatus.CLOSED)
        for st in ContractStatus:
            assert not c.can_transition_to(st)

    def test_rejected_can_be_resubmitted(self) -> None:
        c = _contract(status=ContractStatus.REJECTED)
        assert c.can_transition_to(ContractStatus.SUBMITTED)
