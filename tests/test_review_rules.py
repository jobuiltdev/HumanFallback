"""Deterministic scoring, flags, and recommendations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from humanfallback.classifier import default_classifier
from humanfallback.contracts import build_contract
from humanfallback.models import (
    AcceptanceCriterion,
    CheckKind,
    CheckOutcome,
    CheckType,
    ClassificationResult,
    ContractStatus,
    EvidenceKind,
    EvidenceOutcome,
    EvidenceRequirement,
    FlagKind,
    Recommendation,
    RemoteSubmission,
    Reward,
    Severity,
    TaskCategory,
    TaskContract,
)
from humanfallback.review import review_submission
from humanfallback.review.rules import (
    ACCEPTABLE_SCORE,
    REJECT_SCORE,
    STRONG_SCORE,
    UNVERIFIED_CREDIT,
    WEIGHT_DELIVERABLES,
    WEIGHT_EVIDENCE,
    WEIGHT_FACTUAL,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)
IMG = "https://cdn.gib.work/media/shot.png"
POST = "https://x.com/worker/status/12345"


def _sub(content: str, media: list[str] | None = None, sid: str = "s1") -> RemoteSubmission:
    return RemoteSubmission(id=sid, task_id="t", status="pending", content=content, submitter="w", media=media or [])


def _built(request: str) -> TaskContract:
    return build_contract(request, default_classifier().classify(request), Reward(amount="1.00"))


def _custom(
    evidence: list[EvidenceRequirement],
    criteria: list[AcceptanceCriterion],
    *,
    title: str = "Custom task",
    description: str = "Do the custom task and report back.",
) -> TaskContract:
    return TaskContract(
        title=title,
        description=description,
        source_request=description,
        classification=ClassificationResult(
            human_required=True, category=TaskCategory.PHYSICAL_ACTION, confidence=0.9, reasons=["r"], classifier="t"
        ),
        acceptance_criteria=criteria,
        evidence_requirements=evidence,
        reward=Reward(amount="1.00"),
        status=ContractStatus.DELEGATED,
    )


def _score_of(review) -> dict[str, tuple[int, int]]:  # noqa: ANN001
    return {c.name: (c.points, c.max_points) for c in review.score_breakdown}


# -- account-access template: url + screenshot, one evidence_present criterion ----


class TestTemplateContract:
    @pytest.fixture
    def contract(self) -> TaskContract:
        return _built("Tweet this announcement and tag @gibwork, then send the link.")

    def test_complete_submission_is_strong_but_needs_judgment(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(f"Tweeted the announcement and tagged @gibwork: {POST}", [IMG]), now=NOW)
        assert r.score == 100
        assert r.recommendation is Recommendation.STRONG
        assert r.human_judgment_required is True  # ac-1 is manual
        assert [e.outcome for e in r.evidence_results] == [EvidenceOutcome.FOUND, EvidenceOutcome.FOUND]
        outcomes = {a.criterion_id: a.outcome for a in r.acceptance_results}
        assert outcomes["ac-1"] is CheckOutcome.NEEDS_HUMAN
        assert outcomes["ac-3"] is CheckOutcome.PASS
        assert r.flags == []
        assert "still need your judgment" in r.summary
        assert r.advisory is True and r.reviewer == "rules-v1" and r.reviewed_at == NOW
        assert _score_of(r) == {
            "required_evidence": (50, 50), "factual_criteria": (30, 30),
            "deliverables": (20, 20), "judgment_criteria": (0, 0),
        }

    def test_missing_one_required_evidence_reduces_predictably(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(f"Posted: {POST}"))  # no screenshot
        assert _score_of(r)["required_evidence"] == (25, 50)  # 50 / 2 items
        assert _score_of(r)["factual_criteria"] == (30, 30)  # ac-3 links only ev-1, which is present
        assert r.score == 75
        assert r.recommendation is Recommendation.INCOMPLETE
        assert [m.id for m in r.missing_requirements if m.kind == "evidence"] == ["ev-2"]
        flag = next(f for f in r.flags if f.code == "MISSING_REQUIRED_EVIDENCE")
        assert flag.severity is Severity.WARNING and flag.kind is FlagKind.FACTUAL

    def test_all_evidence_missing_is_critical(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub("I posted the tweet as you asked and tagged the account, it went well and got some likes already today."))
        flag = next(f for f in r.flags if f.code == "MISSING_REQUIRED_EVIDENCE")
        assert flag.severity is Severity.CRITICAL
        assert r.recommendation is Recommendation.REJECT_CANDIDATE
        assert all(a.outcome is CheckOutcome.BLOCKED for a in r.acceptance_results if a.kind is CheckKind.JUDGMENT)

    def test_empty_submission(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(""))
        assert r.has_flag("EMPTY_SUBMISSION")
        assert r.recommendation is Recommendation.REJECT_CANDIDATE
        assert r.score == 0
        assert {d.name: d.outcome for d in r.deliverable_results}["content_present"] == "fail"

    def test_filler_only(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub("Done!"))
        assert r.has_flag("FILLER_ONLY") and r.has_flag("EMPTY_SUBMISSION")

    def test_near_empty(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub("I did the thing yesterday."))
        assert r.has_flag("NEAR_EMPTY") and not r.has_flag("EMPTY_SUBMISSION")

    def test_unverified_type_gets_half_credit_and_forces_human_review(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(f"Posted {POST}", ["0a3e0b1c-media-id"]))
        ev2 = next(e for e in r.evidence_results if e.evidence_id == "ev-2")
        assert ev2.outcome is EvidenceOutcome.FOUND_UNVERIFIED_TYPE
        assert _score_of(r)["required_evidence"] == (round((1 + UNVERIFIED_CREDIT) / 2 * 50), 50)
        assert r.recommendation is Recommendation.NEEDS_HUMAN_REVIEW
        flag = next(f for f in r.flags if f.code == "UNVERIFIED_EVIDENCE_TYPE")
        assert flag.kind is FlagKind.INFERRED and flag.severity is Severity.WARNING

    def test_duplicate_evidence_is_factual_and_collapsed(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(f"See {POST} and {POST}?utm_source=x", [IMG]))
        flag = next(f for f in r.flags if f.code == "DUPLICATE_EVIDENCE")
        assert flag.kind is FlagKind.FACTUAL and flag.severity is Severity.WARNING
        assert r.score == 100  # duplicates never change the score
        assert r.recommendation is Recommendation.NEEDS_HUMAN_REVIEW

    def test_duplicates_cannot_satisfy_two_requirements(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="a"),
             EvidenceRequirement(id="ev-2", kind=EvidenceKind.URL, description="b")],
            [AcceptanceCriterion(id="ac-1", statement="x", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1", "ev-2"])],
        )
        r = review_submission(contract, _sub(f"{POST} {POST}"))
        outcomes = [e.outcome for e in r.evidence_results]
        assert outcomes == [EvidenceOutcome.FOUND, EvidenceOutcome.MISSING]
        assert r.recommendation is Recommendation.INCOMPLETE

    def test_irrelevant_content_is_inferred_and_does_not_change_score(self, contract: TaskContract) -> None:
        prose = "The weather in Lisbon is sunny with a light breeze from the Atlantic, ideal for a long walk by the river and coffee afterwards."
        r = review_submission(contract, _sub(prose + f" {POST}", [IMG]))
        flag = next(f for f in r.flags if f.code == "IRRELEVANT_CONTENT")
        assert flag.kind is FlagKind.INFERRED and flag.severity is Severity.WARNING
        assert "0 of" in flag.message
        assert r.score == 100
        assert r.recommendation is Recommendation.NEEDS_HUMAN_REVIEW

    def test_short_submission_not_judged_for_relevance(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(f"Here: {POST}", [IMG]))
        assert not r.has_flag("IRRELEVANT_CONTENT") and not r.has_flag("LOW_RELEVANCE")

    def test_low_relevance_is_info(self) -> None:
        contract = _built("Tweet this product announcement and tag @gibwork, then send me the link and a screenshot.")
        prose = ("announcement " + "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt "
                 "ut labore et dolore magna aliqua quis nostrud exercitation ullamco laboris nisi")
        r = review_submission(contract, _sub(prose + f" {POST}", [IMG]))
        assert r.has_flag("LOW_RELEVANCE")
        assert r.recommendation is Recommendation.STRONG  # info never demotes

    def test_unsupported_media_type_flagged(self, contract: TaskContract) -> None:
        r = review_submission(contract, _sub(f"{POST}", [IMG, "archive.exe"]))
        flag = next(f for f in r.flags if f.code == "UNSUPPORTED_EVIDENCE_TYPE")
        assert flag.severity is Severity.INFO and flag.kind is FlagKind.FACTUAL
        assert r.recommendation is Recommendation.STRONG


# -- constraints and criteria types -------------------------------------------------


class TestConstraintsAndCriteria:
    def test_min_words_constraint_failure(self) -> None:
        contract = _built("Which one looks better, the blue logo or the green one?")  # subjective: text >= 50 words
        r = review_submission(contract, _sub("The blue one, it is cleaner."))
        ev = r.evidence_results[0]
        assert ev.outcome is EvidenceOutcome.FOUND_CONSTRAINT_FAILED
        assert ev.constraint_results[0].name == "min_words" and ev.constraint_results[0].passed is False
        assert r.has_flag("CONSTRAINT_FAILED")
        assert r.recommendation is Recommendation.INCOMPLETE
        assert _score_of(r)["required_evidence"][0] == 25  # half credit of 50

    def test_min_words_satisfied(self) -> None:
        contract = _built("Which one looks better, the blue logo or the green one?")
        prose = "I prefer the blue logo because " + "it reads clearly at small sizes and matches the palette " * 6
        r = review_submission(contract, _sub(prose))
        assert r.evidence_results[0].outcome is EvidenceOutcome.FOUND
        assert r.recommendation is Recommendation.STRONG

    def test_url_pattern_and_domain(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="x", constraints={"domain": "x.com", "url_pattern": r"/status/\d+"})],
            [AcceptanceCriterion(id="ac-1", statement="c", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1"])],
        )
        good = review_submission(contract, _sub(POST))
        assert good.evidence_results[0].outcome is EvidenceOutcome.FOUND
        bad = review_submission(contract, _sub("https://example.org/post/1"))
        assert bad.evidence_results[0].outcome is EvidenceOutcome.FOUND_CONSTRAINT_FAILED
        failed = [c.name for c in bad.evidence_results[0].constraint_results if not c.passed]
        assert failed == ["domain", "url_pattern"]

    def test_exact_match_and_pattern_criteria(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.TEXT, description="answer")],
            [
                AcceptanceCriterion(id="ac-1", statement="exact", check_type=CheckType.EXACT_MATCH, expected="  Forty Two ", evidence_ids=["ev-1"]),
                AcceptanceCriterion(id="ac-2", statement="pattern", check_type=CheckType.PATTERN, expected=r"forty[- ]two", evidence_ids=["ev-1"]),
                AcceptanceCriterion(id="ac-3", statement="bad pattern", check_type=CheckType.PATTERN, expected="(", evidence_ids=["ev-1"]),
            ],
        )
        r = review_submission(contract, _sub("forty two"))
        outcomes = {a.criterion_id: a.outcome for a in r.acceptance_results}
        assert outcomes == {"ac-1": CheckOutcome.PASS, "ac-2": CheckOutcome.PASS, "ac-3": CheckOutcome.FAIL}
        assert "invalid pattern" in r.acceptance_results[2].detail
        assert all(a.kind is CheckKind.FACTUAL for a in r.acceptance_results)

    def test_transaction_evidence(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.TRANSACTION, description="tx")],
            [AcceptanceCriterion(id="ac-1", statement="c", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1"])],
        )
        assert review_submission(contract, _sub("https://solscan.io/tx/abc")).evidence_results[0].outcome is EvidenceOutcome.FOUND
        assert review_submission(contract, _sub("5VfYmGB7qXhQ" + "1" * 76)).evidence_results[0].outcome is EvidenceOutcome.FOUND
        assert review_submission(contract, _sub(POST)).evidence_results[0].outcome is EvidenceOutcome.MISSING

    def test_file_evidence_prefers_documents(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.FILE, description="pdf", constraints={"extension": "pdf"})],
            [AcceptanceCriterion(id="ac-1", statement="c", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1"])],
        )
        r = review_submission(contract, _sub("signed", ["https://cdn/x.png", "https://cdn/lease.pdf"]))
        assert r.evidence_results[0].outcome is EvidenceOutcome.FOUND
        assert r.evidence_results[0].matched == ["https://cdn/lease.pdf"]

    def test_optional_evidence_not_scored_but_reported(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="req"),
             EvidenceRequirement(id="ev-2", kind=EvidenceKind.PHOTO, description="opt", required=False)],
            [AcceptanceCriterion(id="ac-1", statement="c", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1"])],
        )
        r = review_submission(contract, _sub(POST))
        assert r.evidence_results[1].outcome is EvidenceOutcome.MISSING
        assert r.score == 100
        assert not r.has_flag("MISSING_REQUIRED_EVIDENCE")
        assert [m.id for m in r.missing_requirements] == ["ev-2"]


# -- weight redistribution and subjective-only ---------------------------------------


class TestRedistribution:
    def test_no_factual_criteria_moves_points_to_evidence(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="u")],
            [AcceptanceCriterion(id="ac-1", statement="judge", check_type=CheckType.MANUAL, evidence_ids=["ev-1"])],
        )
        r = review_submission(contract, _sub(POST))
        assert _score_of(r)["required_evidence"] == (WEIGHT_EVIDENCE + WEIGHT_FACTUAL, WEIGHT_EVIDENCE + WEIGHT_FACTUAL)
        assert _score_of(r)["factual_criteria"] == (0, 0)
        assert "redistributed" in next(c.reason for c in r.score_breakdown if c.name == "required_evidence")
        assert r.score == 100 and r.recommendation is Recommendation.STRONG
        assert r.human_judgment_required is True

    def test_no_required_evidence_moves_points_to_criteria(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.TEXT, description="t", required=False)],
            [AcceptanceCriterion(id="ac-1", statement="p", check_type=CheckType.PATTERN, expected="hello")],
        )
        r = review_submission(contract, _sub("hello there friend, this is a proper reply with enough words in it"))
        assert _score_of(r)["factual_criteria"] == (WEIGHT_EVIDENCE + WEIGHT_FACTUAL, WEIGHT_EVIDENCE + WEIGHT_FACTUAL)
        assert _score_of(r)["required_evidence"] == (0, 0)
        assert "redistributed" in next(c.reason for c in r.score_breakdown if c.name == "factual_criteria")
        assert r.score == 100
        assert r.human_judgment_required is False

    def test_subjective_only_never_presents_perfect_score(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.TEXT, description="t", required=False)],
            [AcceptanceCriterion(id="ac-1", statement="judge", check_type=CheckType.MANUAL)],
        )
        r = review_submission(contract, _sub("A thoughtful and complete answer to the question that was asked of me."))
        assert r.has_flag("SUBJECTIVE_ONLY")
        assert r.score == WEIGHT_DELIVERABLES  # only content checks can be earned
        unverifiable = next(c for c in r.score_breakdown if c.name == "unverifiable")
        assert (unverifiable.points, unverifiable.max_points) == (0, WEIGHT_EVIDENCE + WEIGHT_FACTUAL)
        assert r.recommendation is Recommendation.NEEDS_HUMAN_REVIEW
        assert r.human_judgment_required is True
        assert "verified mechanically" in r.summary

    def test_subjective_only_but_empty_is_reject(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.TEXT, description="t", required=False)],
            [AcceptanceCriterion(id="ac-1", statement="judge", check_type=CheckType.MANUAL)],
        )
        r = review_submission(contract, _sub(""))
        assert r.has_flag("SUBJECTIVE_ONLY") and r.has_flag("EMPTY_SUBMISSION")
        assert r.recommendation is Recommendation.REJECT_CANDIDATE
        assert r.score == 0


# -- recommendation thresholds ------------------------------------------------------


class TestRecommendationOrder:
    def test_thresholds_are_consistent(self) -> None:
        assert REJECT_SCORE < ACCEPTABLE_SCORE < STRONG_SCORE <= 100

    def test_acceptable_band(self) -> None:
        # Two required urls + two factual criteria; supply one url -> ev 25/50, one criterion passes 15/30, deliverables 20 => 60 -> incomplete.
        # Use optional second url instead so nothing required is missing, and make one factual criterion fail on its own.
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="u")],
            [
                AcceptanceCriterion(id="ac-1", statement="c", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1"]),
                AcceptanceCriterion(id="ac-2", statement="p", check_type=CheckType.PATTERN, expected="zzz", required=False),
            ],
        )
        r = review_submission(contract, _sub(f"posted {POST}"))
        assert r.score == 85  # 50 + 15 + 20
        assert r.recommendation is Recommendation.ACCEPTABLE

    def test_incomplete_beats_thresholds(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="u"),
             EvidenceRequirement(id="ev-2", kind=EvidenceKind.URL, description="u2")],
            [AcceptanceCriterion(id="ac-1", statement="c", check_type=CheckType.EVIDENCE_PRESENT, evidence_ids=["ev-1"])],
        )
        r = review_submission(contract, _sub(POST))
        assert r.score == 75  # 25 + 30 + 20
        assert r.recommendation is Recommendation.INCOMPLETE

    def test_blocked_judgment_counts_as_incomplete(self) -> None:
        contract = _custom(
            [EvidenceRequirement(id="ev-1", kind=EvidenceKind.URL, description="u", required=False)],
            [
                AcceptanceCriterion(id="ac-1", statement="p", check_type=CheckType.PATTERN, expected="hello"),
                AcceptanceCriterion(id="ac-2", statement="judge", check_type=CheckType.MANUAL, evidence_ids=["ev-1"]),
            ],
        )
        r = review_submission(contract, _sub("hello world everyone"))
        assert r.acceptance_results[1].outcome is CheckOutcome.BLOCKED
        assert r.recommendation is Recommendation.INCOMPLETE

    def test_review_is_deterministic(self) -> None:
        contract = _built("Tweet this announcement and tag @gibwork.")
        sub = _sub(f"tagged {POST}", [IMG])
        a = review_submission(contract, sub, now=NOW)
        b = review_submission(contract, sub, now=NOW)
        assert a == b
