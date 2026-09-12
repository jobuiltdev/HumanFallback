from __future__ import annotations

from datetime import UTC, datetime, timedelta

from humanfallback.classifier import default_classifier
from humanfallback.contracts import build_contract
from humanfallback.models import Recommendation, RemoteSubmission, Reward
from humanfallback.review import HUMAN_ACTION, compare_reviews, review_submission

IMG = "https://cdn.gib.work/media/shot.png"
POST = "https://x.com/worker/status/12345"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _contract():  # noqa: ANN202
    req = "Tweet this announcement and tag @gibwork, then send the link."
    return build_contract(req, default_classifier().classify(req), Reward(amount="1.00"))


def _sub(sid: str, content: str, media: list[str] | None = None, minutes: int = 0) -> RemoteSubmission:
    return RemoteSubmission(
        id=sid, task_id="t", status="pending", content=content, submitter=sid, media=media or [],
        created_at=T0 + timedelta(minutes=minutes),
    )


def _compare(subs: list[RemoteSubmission]):  # noqa: ANN202
    c = _contract()
    reviews = [review_submission(c, s) for s in subs]
    return compare_reviews(c.id, reviews, subs)


def test_ranks_and_picks_unique_strong_top() -> None:
    subs = [
        _sub("weak", "done"),
        _sub("full", f"Tweeted and tagged @gibwork: {POST}", [IMG]),
        _sub("partial", f"Posted {POST}"),
    ]
    cmp = _compare(subs)
    assert [r.submission_id for r in cmp.ranked_reviews] == ["full", "partial", "weak"]
    assert [r.rank for r in cmp.ranked_reviews] == [1, 2, 3]
    assert cmp.strongest_submission_id == "full"
    assert any("ranks first" in n for n in cmp.comparison_notes)
    assert cmp.human_action_required == HUMAN_ACTION
    assert cmp.advisory is True


def test_full_tie_yields_no_strongest() -> None:
    subs = [
        _sub("a", f"Tweeted and tagged @gibwork: {POST}", [IMG], minutes=1),
        _sub("b", f"Tagged @gibwork announcement here https://x.com/other/status/2", ["https://cdn/o.png"], minutes=0),
    ]
    cmp = _compare(subs)
    assert cmp.strongest_submission_id is None
    assert any("tie on score" in n for n in cmp.comparison_notes)
    assert [r.submission_id for r in cmp.ranked_reviews] == ["b", "a"]  # earlier submission first on a tie


def test_same_score_broken_by_flags() -> None:
    prose = "The weather in Lisbon is sunny with a light breeze from the Atlantic, ideal for a long walk by the river and coffee afterwards."
    subs = [
        _sub("irrelevant", f"{prose} {POST}", [IMG], minutes=0),
        _sub("clean", f"Tweeted and tagged @gibwork: https://x.com/w/status/7", ["https://cdn/c.png"], minutes=1),
    ]
    cmp = _compare(subs)
    assert [r.submission_id for r in cmp.ranked_reviews] == ["clean", "irrelevant"]
    assert cmp.strongest_submission_id == "clean"
    assert any("share score" in n for n in cmp.comparison_notes)


def test_top_needing_human_review_is_not_chosen() -> None:
    subs = [_sub("unverified", f"Posted {POST}", ["media-id-only"])]
    cmp = _compare(subs)
    assert cmp.ranked_reviews[0].recommendation is Recommendation.NEEDS_HUMAN_REVIEW
    assert cmp.strongest_submission_id is None
    assert any("no strongest submission chosen" in n for n in cmp.comparison_notes)


def test_all_incomplete_and_reject_top() -> None:
    cmp = _compare([_sub("x", f"Posted {POST}"), _sub("y", "https://x.com/y/status/1 posted")])
    assert cmp.strongest_submission_id is None
    assert any("every submission is incomplete" in n for n in cmp.comparison_notes)
    cmp = _compare([_sub("empty", "")])
    assert any("reject candidate" in n for n in cmp.comparison_notes)


def test_shared_evidence_across_submissions_is_noted() -> None:
    subs = [
        _sub("a", f"Mine: {POST}", [IMG]),
        _sub("b", f"Also mine: {POST}?utm_source=copy", ["https://cdn/b.png"]),
    ]
    cmp = _compare(subs)
    shared = [n for n in cmp.comparison_notes if n.startswith("SHARED_EVIDENCE_ACROSS_SUBMISSIONS")]
    assert len(shared) == 1
    assert "a, b" in shared[0]


def test_empty_input() -> None:
    cmp = compare_reviews("c", [], [])
    assert cmp.ranked_reviews == [] and cmp.strongest_submission_id is None
    assert cmp.comparison_notes == ["no submissions to compare"]
