"""Rank several reviews of the same contract and note what a person should
look at across them. Never picks a winner when the evidence is ambiguous."""

from __future__ import annotations

from collections import defaultdict

from humanfallback.models import (
    Recommendation,
    RemoteSubmission,
    Severity,
    SubmissionComparison,
    SubmissionReview,
)

from .extract import extract
from .rules import ACCEPTABLE_SCORE

HUMAN_ACTION = (
    "Review the ranked scorecards, then approve or reject submissions in Gibwork yourself. "
    "HumanFallback does not approve, reject, or pay."
)


def _sort_key(review: SubmissionReview, created_order: dict[str, int]) -> tuple:
    return (
        -review.score,
        len(review.missing_requirements),
        len(review.flags_with(Severity.CRITICAL)),
        len(review.flags_with(Severity.WARNING)),
        created_order.get(review.submission_id, 0),
    )


def compare_reviews(
    contract_id: str,
    reviews: list[SubmissionReview],
    submissions: list[RemoteSubmission] | None = None,
) -> SubmissionComparison:
    subs = submissions or []
    ordered = sorted(subs, key=lambda s: (s.created_at is None, s.created_at))
    created_order = {s.id: i for i, s in enumerate(ordered)}

    ranked = sorted(reviews, key=lambda r: _sort_key(r, created_order))
    for i, review in enumerate(ranked, start=1):
        review.rank = i

    notes: list[str] = []
    strongest: str | None = None
    if not ranked:
        notes.append("no submissions to compare")
    else:
        top = ranked[0]
        top_key = _sort_key(top, created_order)[:-1]
        tied = [r for r in ranked if _sort_key(r, created_order)[:-1] == top_key]
        same_score = [r for r in ranked if r.score == top.score]
        if len(tied) > 1:
            notes.append(
                f"{len(tied)} submissions tie on score, missing items, and flags (score {top.score}): "
                + ", ".join(r.submission_id for r in tied)
            )
        elif len(same_score) > 1:
            notes.append(
                f"{len(same_score)} submissions share score {top.score}; {top.submission_id} ranks first "
                "because it has fewer missing items or flags"
            )
        if top.recommendation is Recommendation.REJECT_CANDIDATE:
            notes.append("the highest-ranked submission is a reject candidate")
        elif top.score < ACCEPTABLE_SCORE:
            notes.append(f"no submission reaches the acceptable score of {ACCEPTABLE_SCORE}")
        elif top.recommendation not in (Recommendation.STRONG, Recommendation.ACCEPTABLE):
            notes.append(
                f"the highest-ranked submission is {top.recommendation.value}; no strongest submission chosen"
            )
        elif len(tied) == 1:
            strongest = top.submission_id
            notes.append(f"{top.submission_id} ranks first on verifiable checks (score {top.score})")
        incomplete = [r for r in ranked if r.recommendation is Recommendation.INCOMPLETE]
        if incomplete and len(incomplete) == len(ranked):
            notes.append("every submission is incomplete on required items")
        judgment = sum(1 for r in ranked if r.human_judgment_required)
        if judgment:
            notes.append(f"{judgment} of {len(ranked)} submissions need a person's judgment on at least one criterion")

    shared: dict[str, list[str]] = defaultdict(list)
    for sub in subs:
        for item in extract(sub).items:
            shared[item.normalized].append(sub.id)
    for value, owners in shared.items():
        if len(set(owners)) > 1:
            notes.append(
                f"SHARED_EVIDENCE_ACROSS_SUBMISSIONS: {value} appears in {len(set(owners))} submissions "
                f"({', '.join(sorted(set(owners)))}); possible copied work"
            )

    return SubmissionComparison(
        contract_id=contract_id,
        ranked_reviews=ranked,
        strongest_submission_id=strongest,
        comparison_notes=notes,
        human_action_required=HUMAN_ACTION,
    )
