"""Deterministic evaluation of one submission against its Task Contract.

Three layers, kept apart in the output:
  factual checks   - evidence found or not, constraints, exact/pattern criteria
  inferred warnings - relevance, unverified evidence types
  recommendation   - derived from the two above by fixed rules

Every score point is attributed to a named component with a reason.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from humanfallback.models import (
    AcceptanceCriterion,
    CheckKind,
    CheckOutcome,
    CheckType,
    ConstraintResult,
    CriterionResult,
    DeliverableResult,
    EvidenceKind,
    EvidenceOutcome,
    EvidenceRequirement,
    EvidenceResult,
    Flag,
    FlagKind,
    MissingRequirement,
    Recommendation,
    RemoteSubmission,
    ScoreComponent,
    Severity,
    SubmissionReview,
    TaskContract,
)

from .extract import EvidenceItem, Extracted, extract
from .text import contract_terms, overlap

# -- tunables (all surfaced in messages so results stay explainable) -------------

WEIGHT_EVIDENCE = 50
WEIGHT_FACTUAL = 30
WEIGHT_DELIVERABLES = 20
UNVERIFIED_CREDIT = 0.5
CONSTRAINT_FAILED_CREDIT = 0.5

SUBSTANTIVE_WORDS = 10
RELEVANCE_MIN_WORDS = 20
LOW_RELEVANCE_RATIO = 0.15
EMPTY_WORDS = 3
STRONG_SCORE = 90
ACCEPTABLE_SCORE = 70
REJECT_SCORE = 30

FILLER_PHRASES = frozenset(
    {"done", "completed", "complete", "finished", "submitted", "ok", "okay", "here", "here you go",
     "see attached", "attached", "as requested", "task done", "done task", "please check", "check"}
)

# Which extracted item kinds satisfy which evidence kinds.
_FULL: dict[EvidenceKind, frozenset[str]] = {
    EvidenceKind.URL: frozenset({"url", "transaction", "image", "document"}),
    EvidenceKind.PHOTO: frozenset({"image"}),
    EvidenceKind.SCREENSHOT: frozenset({"image"}),
    EvidenceKind.FILE: frozenset({"document"}),
    EvidenceKind.TRANSACTION: frozenset({"transaction"}),
}
_UNVERIFIED: dict[EvidenceKind, frozenset[str]] = {
    EvidenceKind.PHOTO: frozenset({"media"}),
    EvidenceKind.SCREENSHOT: frozenset({"media"}),
    EvidenceKind.FILE: frozenset({"media", "image"}),
}

FACTUAL_CHECKS = (CheckType.EVIDENCE_PRESENT, CheckType.EXACT_MATCH, CheckType.PATTERN)


def counted_factual(criteria: list[CriterionResult]) -> list[CriterionResult]:
    """Factual criteria that take part in the score: optional ones whose
    input was never supplied are left out of both numerator and denominator."""
    return [c for c in criteria if c.kind is CheckKind.FACTUAL and c.outcome is not CheckOutcome.NOT_APPLICABLE]


# -- evidence ---------------------------------------------------------------------


def _check_constraints(req: EvidenceRequirement, item: EvidenceItem | None, ex: Extracted) -> list[ConstraintResult]:
    results: list[ConstraintResult] = []
    for name, expected in req.constraints.items():
        if name == "min_words":
            actual = ex.word_count
            results.append(ConstraintResult(name=name, expected=expected, actual=str(actual), passed=actual >= int(expected)))
        elif name == "max_words":
            actual = ex.word_count
            results.append(ConstraintResult(name=name, expected=expected, actual=str(actual), passed=actual <= int(expected)))
        elif name == "url_pattern" and item is not None:
            passed = re.search(expected, item.value) is not None
            results.append(ConstraintResult(name=name, expected=expected, actual=item.value, passed=passed))
        elif name == "domain" and item is not None:
            host = item.host or ""
            passed = host == expected.lower() or host.endswith("." + expected.lower())
            results.append(ConstraintResult(name=name, expected=expected, actual=host or "-", passed=passed))
        elif name == "extension" and item is not None:
            actual = item.extension or "-"
            results.append(ConstraintResult(name=name, expected=expected, actual=actual, passed=actual == expected.lower()))
        else:
            results.append(ConstraintResult(name=name, expected=expected, actual="not checkable", passed=True))
    return results


def evaluate_evidence(contract: TaskContract, ex: Extracted) -> list[EvidenceResult]:
    """Match extracted items to requirements: required first, contract order,
    each item consumed at most once, full-credit kinds before unverified."""
    consumed: set[str] = set()
    results: dict[str, EvidenceResult] = {}
    ordered = sorted(contract.evidence_requirements, key=lambda r: (not r.required,))

    def take(kinds: frozenset[str]) -> EvidenceItem | None:
        for item in ex.items:
            if item.normalized not in consumed and item.kind in kinds:
                consumed.add(item.normalized)
                return item
        return None

    for req in ordered:
        if req.kind is EvidenceKind.TEXT:
            if ex.word_count > 0:
                constraints = _check_constraints(req, None, ex)
                failed = [c for c in constraints if not c.passed]
                outcome = EvidenceOutcome.FOUND_CONSTRAINT_FAILED if failed else EvidenceOutcome.FOUND
                detail = f"{ex.word_count} words of text" + (
                    "; " + ", ".join(f"{c.name} expected {c.expected}, got {c.actual}" for c in failed) if failed else ""
                )
                results[req.id] = EvidenceResult(
                    evidence_id=req.id, kind=req.kind, required=req.required, outcome=outcome,
                    matched=[ex.body[:120]], constraint_results=constraints, detail=detail,
                )
            else:
                results[req.id] = EvidenceResult(
                    evidence_id=req.id, kind=req.kind, required=req.required,
                    outcome=EvidenceOutcome.MISSING, detail="no text body in the submission",
                )
            continue

        item = take(_FULL.get(req.kind, frozenset()))
        unverified = False
        if item is None and req.kind in _UNVERIFIED:
            item = take(_UNVERIFIED[req.kind])
            unverified = item is not None
        if item is None:
            results[req.id] = EvidenceResult(
                evidence_id=req.id, kind=req.kind, required=req.required,
                outcome=EvidenceOutcome.MISSING, detail=f"no {req.kind.value} evidence found",
            )
            continue
        constraints = _check_constraints(req, item, ex)
        failed = [c for c in constraints if not c.passed]
        if failed:
            outcome = EvidenceOutcome.FOUND_CONSTRAINT_FAILED
            detail = "; ".join(f"{c.name} expected {c.expected}, got {c.actual}" for c in failed)
        elif unverified:
            outcome = EvidenceOutcome.FOUND_UNVERIFIED_TYPE
            detail = (
                f"{item.value} is present but its type cannot be confirmed as {req.kind.value} "
                f"(classified as {item.kind})"
            )
        else:
            outcome = EvidenceOutcome.FOUND
            where = " (inline <img> in content)" if item.source == "inline_image" else ""
            detail = f"{item.kind} evidence {item.value}{where}"
        results[req.id] = EvidenceResult(
            evidence_id=req.id, kind=req.kind, required=req.required, outcome=outcome,
            matched=[item.value], constraint_results=constraints, detail=detail,
        )

    return [results[r.id] for r in contract.evidence_requirements]


# -- criteria ---------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def evaluate_criteria(
    contract: TaskContract, ex: Extracted, evidence: dict[str, EvidenceResult]
) -> list[CriterionResult]:
    results: list[CriterionResult] = []
    for ac in contract.acceptance_criteria:
        linked = [evidence[e] for e in ac.evidence_ids if e in evidence]
        linked_ok = all(r.outcome is not EvidenceOutcome.MISSING for r in linked)
        missing_ids = [r.evidence_id for r in linked if r.outcome is EvidenceOutcome.MISSING]

        if ac.check_type is CheckType.MANUAL:
            if linked and not linked_ok:
                outcome, detail = CheckOutcome.BLOCKED, f"cannot be judged: evidence {', '.join(missing_ids)} missing"
            else:
                outcome, detail = CheckOutcome.NEEDS_HUMAN, "requires a person's judgment"
            kind = CheckKind.JUDGMENT
        elif ac.check_type is CheckType.EVIDENCE_PRESENT:
            kind = CheckKind.FACTUAL
            if not linked:
                outcome, detail = CheckOutcome.FAIL, "criterion links no evidence to check"
            elif linked_ok:
                outcome, detail = CheckOutcome.PASS, f"evidence {', '.join(ac.evidence_ids)} present"
            else:
                outcome, detail = CheckOutcome.FAIL, f"evidence {', '.join(missing_ids)} missing"
        elif ac.check_type is CheckType.EXACT_MATCH:
            kind = CheckKind.FACTUAL
            expected = _norm(ac.expected or "")
            if expected and _norm(ex.body) == expected:
                outcome, detail = CheckOutcome.PASS, "text matches expected value"
            else:
                outcome, detail = CheckOutcome.FAIL, f"text does not equal expected {ac.expected!r}"
        else:  # PATTERN
            kind = CheckKind.FACTUAL
            try:
                found = bool(ac.expected) and re.search(ac.expected or "", ex.body, re.IGNORECASE) is not None
                detail = f"pattern {ac.expected!r} {'found' if found else 'not found'} in text"
            except re.error as exc:
                found, detail = False, f"invalid pattern {ac.expected!r}: {exc}"
            outcome = CheckOutcome.PASS if found else CheckOutcome.FAIL

        if not ac.required and outcome in (CheckOutcome.FAIL, CheckOutcome.BLOCKED):
            # An optional criterion whose input was not supplied is skipped: it
            # is neither missing nor counted. Supplied inputs are judged as usual.
            outcome = CheckOutcome.NOT_APPLICABLE
            detail = f"optional and not supplied ({detail}); not counted"

        results.append(
            CriterionResult(
                criterion_id=ac.id, statement=ac.statement, check_type=ac.check_type,
                required=ac.required, kind=kind, outcome=outcome, evidence_ids=list(ac.evidence_ids), detail=detail,
            )
        )
    return results


# -- deliverables and flags -------------------------------------------------------


def evaluate_deliverables(ex: Extracted, evidence: list[EvidenceResult]) -> list[DeliverableResult]:
    required = [r for r in evidence if r.required]
    supplied = all(r.outcome is not EvidenceOutcome.MISSING for r in required)
    constraint_ok = all(c.passed for r in evidence for c in r.constraint_results)
    present = ex.word_count > 0 or bool(ex.items)
    substantive = ex.word_count >= SUBSTANTIVE_WORDS or bool(ex.items)
    return [
        DeliverableResult(
            name="content_present", outcome="pass" if present else "fail",
            detail=f"{ex.word_count} words, {len(ex.items)} evidence items",
        ),
        DeliverableResult(
            name="content_substantive", outcome="pass" if substantive else "fail",
            detail=f"needs >= {SUBSTANTIVE_WORDS} words or at least one evidence item",
        ),
        DeliverableResult(
            name="required_evidence_supplied", outcome="pass" if supplied else "fail",
            detail=f"{sum(1 for r in required if r.outcome is not EvidenceOutcome.MISSING)} of {len(required)} required items found",
        ),
        DeliverableResult(
            name="constraints_satisfied", outcome="pass" if constraint_ok else "fail",
            detail="all evidence constraints met" if constraint_ok else "one or more evidence constraints failed",
        ),
    ]


def _is_filler(body: str) -> bool:
    normalized = re.sub(r"[^a-z ]", " ", body.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False
    if normalized in FILLER_PHRASES:
        return True
    words = normalized.split()
    return len(words) <= 3 and all(w in FILLER_PHRASES for w in words)


def evaluate_flags(
    contract: TaskContract,
    ex: Extracted,
    evidence: list[EvidenceResult],
    criteria: list[CriterionResult],
) -> list[Flag]:
    flags: list[Flag] = []
    required = [r for r in evidence if r.required]
    missing_required = [r for r in required if r.outcome is EvidenceOutcome.MISSING]
    factual_criteria = [c for c in criteria if c.kind is CheckKind.FACTUAL]

    # factual
    if ex.word_count < EMPTY_WORDS and not ex.items:
        flags.append(Flag(code="EMPTY_SUBMISSION", severity=Severity.CRITICAL, kind=FlagKind.FACTUAL,
                          message=f"fewer than {EMPTY_WORDS} words and no links or media"))
    elif ex.word_count < SUBSTANTIVE_WORDS and not ex.items:
        flags.append(Flag(code="NEAR_EMPTY", severity=Severity.WARNING, kind=FlagKind.FACTUAL,
                          message=f"only {ex.word_count} words and no evidence items"))
    if not ex.items and _is_filler(ex.body):
        flags.append(Flag(code="FILLER_ONLY", severity=Severity.WARNING, kind=FlagKind.FACTUAL,
                          message=f"body is only a filler phrase ({ex.body!r}) with no evidence"))
    if missing_required:
        all_missing = len(missing_required) == len(required)
        flags.append(Flag(
            code="MISSING_REQUIRED_EVIDENCE",
            severity=Severity.CRITICAL if all_missing else Severity.WARNING,
            kind=FlagKind.FACTUAL,
            message=f"{len(missing_required)} of {len(required)} required evidence items missing",
            evidence=[r.evidence_id for r in missing_required],
        ))
    failed_constraints = [(r.evidence_id, c) for r in evidence for c in r.constraint_results if not c.passed]
    if failed_constraints:
        flags.append(Flag(
            code="CONSTRAINT_FAILED", severity=Severity.WARNING, kind=FlagKind.FACTUAL,
            message="; ".join(f"{eid}: {c.name} expected {c.expected}, got {c.actual}" for eid, c in failed_constraints),
            evidence=[eid for eid, _ in failed_constraints],
        ))
    if ex.duplicates:
        flags.append(Flag(
            code="DUPLICATE_EVIDENCE", severity=Severity.WARNING, kind=FlagKind.FACTUAL,
            message="same evidence referenced more than once: "
            + ", ".join(f"{v} x{n}" for v, n in ex.duplicates.items())
            + "; duplicates were collapsed before matching",
            evidence=list(ex.duplicates),
        ))
    if ex.unsupported:
        flags.append(Flag(
            code="UNSUPPORTED_EVIDENCE_TYPE", severity=Severity.INFO, kind=FlagKind.FACTUAL,
            message="media with an unrecognised type: " + ", ".join(ex.unsupported),
            evidence=list(ex.unsupported),
        ))

    # inferred
    unverified = [r for r in evidence if r.outcome is EvidenceOutcome.FOUND_UNVERIFIED_TYPE]
    if unverified:
        flags.append(Flag(
            code="UNVERIFIED_EVIDENCE_TYPE",
            severity=Severity.WARNING if any(r.required for r in unverified) else Severity.INFO,
            kind=FlagKind.INFERRED,
            message="evidence counted whose type could not be confirmed: "
            + ", ".join(f"{r.evidence_id} ({r.kind.value})" for r in unverified),
            evidence=[r.evidence_id for r in unverified],
        ))
    terms = contract_terms(contract.title, contract.description)
    if ex.word_count >= RELEVANCE_MIN_WORDS and terms:
        matched, total, ratio = overlap(ex.body, terms)
        if matched == 0:
            flags.append(Flag(code="IRRELEVANT_CONTENT", severity=Severity.WARNING, kind=FlagKind.INFERRED,
                              message=f"0 of {total} request terms appear in {ex.word_count} words of text"))
        elif ratio < LOW_RELEVANCE_RATIO:
            flags.append(Flag(code="LOW_RELEVANCE", severity=Severity.INFO, kind=FlagKind.INFERRED,
                              message=f"only {matched} of {total} request terms appear in the text "
                                      f"(ratio {ratio:.2f} < {LOW_RELEVANCE_RATIO})"))
    if not required and not factual_criteria:
        flags.append(Flag(
            code="SUBJECTIVE_ONLY", severity=Severity.INFO, kind=FlagKind.INFERRED,
            message="the contract has no required evidence and no factual criteria; "
                    "nothing about this submission can be verified mechanically",
        ))
    return flags


# -- score ------------------------------------------------------------------------


def _evidence_credit(r: EvidenceResult) -> float:
    if r.outcome is EvidenceOutcome.FOUND:
        return 1.0
    if r.outcome is EvidenceOutcome.FOUND_UNVERIFIED_TYPE:
        return UNVERIFIED_CREDIT
    if r.outcome is EvidenceOutcome.FOUND_CONSTRAINT_FAILED:
        return CONSTRAINT_FAILED_CREDIT
    return 0.0


def score(evidence: list[EvidenceResult], criteria: list[CriterionResult], deliverables: list[DeliverableResult]) -> tuple[int, list[ScoreComponent]]:
    required = [r for r in evidence if r.required]
    factual = counted_factual(criteria)
    skipped = [c for c in criteria if c.outcome is CheckOutcome.NOT_APPLICABLE]
    components: list[ScoreComponent] = []

    ev_max, fc_max = WEIGHT_EVIDENCE, WEIGHT_FACTUAL
    ev_note = fc_note = ""
    if required and not factual:
        ev_max, fc_max = WEIGHT_EVIDENCE + WEIGHT_FACTUAL, 0
        why = "none supplied" if skipped else "none defined"
        ev_note = f"; {WEIGHT_FACTUAL} points redistributed from factual criteria ({why})"
    elif factual and not required:
        ev_max, fc_max = 0, WEIGHT_EVIDENCE + WEIGHT_FACTUAL
        fc_note = f"; {WEIGHT_EVIDENCE} points redistributed from required evidence (none defined)"
    elif not required and not factual:
        ev_max = fc_max = 0
    if skipped:
        fc_note += "; optional not supplied and not counted: " + ", ".join(c.criterion_id for c in skipped)

    if required:
        earned = sum(_evidence_credit(r) for r in required) / len(required) * ev_max
        parts = ", ".join(f"{r.evidence_id}={_evidence_credit(r):.0%}" for r in required)
        components.append(ScoreComponent(
            name="required_evidence", points=round(earned), max_points=ev_max,
            reason=f"{ev_max}/{len(required)} points per required item; {parts}{ev_note}",
        ))
    else:
        components.append(ScoreComponent(name="required_evidence", points=0, max_points=0,
                                         reason="no required evidence defined on the contract"))

    if factual:
        passed = [c for c in factual if c.outcome is CheckOutcome.PASS]
        earned = len(passed) / len(factual) * fc_max
        components.append(ScoreComponent(
            name="factual_criteria", points=round(earned), max_points=fc_max,
            reason=f"{len(passed)} of {len(factual)} factual criteria pass ({fc_max}/{len(factual)} each){fc_note}",
        ))
    else:
        components.append(ScoreComponent(
            name="factual_criteria", points=0, max_points=0,
            reason=("no factual criteria defined on the contract" if not skipped else
                    "no factual criteria to count" + fc_note),
        ))

    by_name = {d.name: d for d in deliverables}
    half = WEIGHT_DELIVERABLES // 2
    present = half if by_name["content_present"].outcome == "pass" else 0
    substantive = half if by_name["content_substantive"].outcome == "pass" else 0
    components.append(ScoreComponent(
        name="deliverables", points=present + substantive, max_points=WEIGHT_DELIVERABLES,
        reason=f"content_present {present}/{half}, content_substantive {substantive}/{half}",
    ))

    if not required and not factual:
        components.append(ScoreComponent(
            name="unverifiable", points=0, max_points=WEIGHT_EVIDENCE + WEIGHT_FACTUAL,
            reason="no required evidence and no factual criteria; these points cannot be earned "
                   "mechanically and are left to human judgment",
        ))
    judgment = [c for c in criteria if c.kind is CheckKind.JUDGMENT]
    if judgment:
        components.append(ScoreComponent(
            name="judgment_criteria", points=0, max_points=0,
            reason=f"{len(judgment)} criteria need a person's judgment and are never scored",
        ))

    total = sum(c.points for c in components)
    return max(0, min(100, total)), components


# -- recommendation ---------------------------------------------------------------


def recommend(
    total: int, evidence: list[EvidenceResult], criteria: list[CriterionResult], flags: list[Flag]
) -> tuple[Recommendation, str]:
    codes = {f.code for f in flags}
    if any(f.severity is Severity.CRITICAL for f in flags):
        crit = ", ".join(f.code for f in flags if f.severity is Severity.CRITICAL)
        return Recommendation.REJECT_CANDIDATE, f"critical issue: {crit}"
    if "SUBJECTIVE_ONLY" in codes:
        return Recommendation.NEEDS_HUMAN_REVIEW, "nothing can be verified mechanically; the score only reflects that content exists"
    if total < REJECT_SCORE:
        return Recommendation.REJECT_CANDIDATE, f"score {total} is below {REJECT_SCORE}"
    required_short = [
        r for r in evidence if r.required and r.outcome in (EvidenceOutcome.MISSING, EvidenceOutcome.FOUND_CONSTRAINT_FAILED)
    ]
    failed = [c for c in criteria if c.required and c.outcome in (CheckOutcome.FAIL, CheckOutcome.BLOCKED)]
    if required_short or failed:
        parts = [r.evidence_id for r in required_short] + [c.criterion_id for c in failed]
        return Recommendation.INCOMPLETE, "required items not satisfied: " + ", ".join(parts)
    unverified_required = [r for r in evidence if r.required and r.outcome is EvidenceOutcome.FOUND_UNVERIFIED_TYPE]
    inferred_warnings = [f for f in flags if f.kind is FlagKind.INFERRED and f.severity is Severity.WARNING]
    if unverified_required:
        return Recommendation.NEEDS_HUMAN_REVIEW, "required evidence present but its type could not be verified: " + ", ".join(r.evidence_id for r in unverified_required)
    if "DUPLICATE_EVIDENCE" in codes:
        return Recommendation.NEEDS_HUMAN_REVIEW, "duplicate evidence references need a look"
    if inferred_warnings:
        return Recommendation.NEEDS_HUMAN_REVIEW, "warnings: " + ", ".join(f.code for f in inferred_warnings)
    if total >= STRONG_SCORE:
        return Recommendation.STRONG, f"all verifiable checks pass (score {total})"
    if total >= ACCEPTABLE_SCORE:
        return Recommendation.ACCEPTABLE, f"verifiable checks mostly pass (score {total})"
    return Recommendation.NEEDS_HUMAN_REVIEW, f"score {total} is between {REJECT_SCORE} and {ACCEPTABLE_SCORE}"


# -- entry point ------------------------------------------------------------------


def review_submission(contract: TaskContract, submission: RemoteSubmission, *, now: datetime | None = None) -> SubmissionReview:
    ex = extract(submission)
    evidence = evaluate_evidence(contract, ex)
    by_id = {r.evidence_id: r for r in evidence}
    criteria = evaluate_criteria(contract, ex, by_id)
    deliverables = evaluate_deliverables(ex, evidence)
    flags = evaluate_flags(contract, ex, evidence, criteria)
    total, breakdown = score(evidence, criteria, deliverables)
    recommendation, why = recommend(total, evidence, criteria, flags)

    missing: list[MissingRequirement] = []
    for r in evidence:
        if r.outcome is EvidenceOutcome.MISSING:
            req = next(e for e in contract.evidence_requirements if e.id == r.evidence_id)
            missing.append(MissingRequirement(kind="evidence", id=r.evidence_id, description=req.description, required=r.required))
    for c in criteria:
        if c.outcome in (CheckOutcome.FAIL, CheckOutcome.BLOCKED):
            missing.append(MissingRequirement(kind="criterion", id=c.criterion_id, description=c.statement, required=c.required))
    for d in deliverables:
        if d.outcome == "fail":
            missing.append(MissingRequirement(kind="deliverable", id=d.name, description=d.detail, required=True))

    judgment = [c for c in criteria if c.kind is CheckKind.JUDGMENT]
    human_required = bool(judgment) or recommendation is Recommendation.NEEDS_HUMAN_REVIEW
    summary = _summary(total, recommendation, why, evidence, criteria, judgment, flags)

    return SubmissionReview(
        submission_id=submission.id,
        contract_id=contract.id,
        submitter=submission.submitter,
        reviewed_at=now or datetime.now(UTC),
        acceptance_results=criteria,
        evidence_results=evidence,
        deliverable_results=deliverables,
        missing_requirements=missing,
        flags=flags,
        score=total,
        score_breakdown=breakdown,
        recommendation=recommendation,
        human_judgment_required=human_required,
        summary=summary,
    )


def _summary(
    total: int,
    recommendation: Recommendation,
    why: str,
    evidence: list[EvidenceResult],
    criteria: list[CriterionResult],
    judgment: list[CriterionResult],
    flags: list[Flag],
) -> str:
    required = [r for r in evidence if r.required]
    found = sum(1 for r in required if r.outcome is not EvidenceOutcome.MISSING)
    factual = counted_factual(criteria)
    passed = sum(1 for c in factual if c.outcome is CheckOutcome.PASS)
    skipped = [c for c in criteria if c.outcome is CheckOutcome.NOT_APPLICABLE]
    parts = [f"Score {total}/100, {recommendation.value}: {why}."]
    if required:
        parts.append(f"Required evidence {found}/{len(required)} found.")
    if factual:
        parts.append(f"Factual criteria {passed}/{len(factual)} pass.")
    if skipped:
        parts.append(f"Optional not supplied: {', '.join(c.criterion_id for c in skipped)}.")
    if judgment:
        parts.append(f"{len(judgment)} criteria still need your judgment; the score does not cover them.")
    warnings = [f.code for f in flags if f.severity is not Severity.INFO]
    if warnings:
        parts.append("Flags: " + ", ".join(warnings) + ".")
    parts.append("Advisory only; approve or reject in Gibwork yourself.")
    return " ".join(parts)
