"""Plain-text helpers for deterministic review: HTML stripping, tokenising,
and a term-overlap relevance measure. Purely lexical; no external services."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    few for from further get got had has have having he her here hers herself him himself
    his how i if in into is it its itself just let me more most my myself no nor not now
    of off on once only or other our ours ourselves out over own per please same she
    should so some such than that the their theirs them themselves then there these they
    this those through to too under until up us very was we were what when where which
    while who whom why will with would you your yours yourself yourselves
    one two three four five first second next last new old
    make made making take taken taking use used using need needs needed want wanted
    go goes going went come came thing things something anything everything
    """.split()
)

MAX_CONTRACT_TERMS = 20


def strip_html(value: str) -> str:
    """Remove tags, unescape entities, collapse whitespace."""
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def stem(token: str) -> str:
    """Light suffix stripping so 'photos' ~ 'photo' and 'tagged' ~ 'tag'."""
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            base = token[: -len(suffix)]
            if suffix == "ies":
                return base + "y"
            if len(base) >= 4 and base[-1] == base[-2] and base[-1] not in "aeiou":
                base = base[:-1]  # tagged -> tag, running -> run
            return base
    return token


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def word_count(text: str) -> int:
    return len([w for w in _WS_RE.split(text.strip()) if w]) if text.strip() else 0


def contract_terms(*sources: str) -> list[str]:
    """Distinct, order-preserving terms from the contract text, capped."""
    seen: list[str] = []
    for source in sources:
        for token in tokenize(source):
            if token not in seen:
                seen.append(token)
    return seen[:MAX_CONTRACT_TERMS]


def overlap(submission_text: str, terms: list[str]) -> tuple[int, int, float]:
    """(matched, total, ratio) of contract terms present in the submission text."""
    if not terms:
        return 0, 0, 0.0
    present = set(tokenize(submission_text))
    matched = sum(1 for t in terms if t in present)
    return matched, len(terms), matched / len(terms)
