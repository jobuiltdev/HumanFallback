"""Pull verifiable evidence out of a submission: URLs, media, transaction
signatures, and the prose body. Purely syntactic."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from humanfallback.models import RemoteSubmission

from .text import strip_html, word_count

ItemKind = str  # url | image | document | media | transaction | unknown

_URL_RE = re.compile(r"""https?://[^\s<>"'\)\]]+""", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)\"'"
_SIGNATURE_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{87,88}(?![1-9A-HJ-NP-Za-km-z])")

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "svg"})
DOCUMENT_EXTENSIONS = frozenset(
    {"pdf", "csv", "json", "txt", "md", "zip", "doc", "docx", "xls", "xlsx", "ppt", "pptx"}
)
EXPLORER_HOSTS = ("solscan.io", "solana.fm", "explorer.solana.com")
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "ref_src", "s", "t")


@dataclass(frozen=True)
class EvidenceItem:
    value: str
    normalized: str
    kind: ItemKind
    source: str  # content | media
    extension: str | None = None

    @property
    def host(self) -> str | None:
        if self.kind in ("url", "transaction") and "://" in self.value:
            return urlsplit(self.value).hostname
        return None


@dataclass
class Extracted:
    body: str  # prose with URLs and signatures removed
    word_count: int
    items: list[EvidenceItem] = field(default_factory=list)  # deduplicated, first occurrence kept
    duplicates: dict[str, int] = field(default_factory=dict)  # normalized value -> occurrences (>1)
    unsupported: list[str] = field(default_factory=list)  # values no extractor could classify


def normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or ""
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAMS[0]) and k.lower() not in _TRACKING_PARAMS[1:]
    ]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(query)), ""))


def _extension(value: str) -> str | None:
    path = urlsplit(value).path if "://" in value else value
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower() or None


def classify_media(value: str) -> tuple[ItemKind, str | None]:
    ext = _extension(value)
    if ext is None:
        return "media", None  # e.g. a Gibwork media id; present but type unknown
    if ext in IMAGE_EXTENSIONS:
        return "image", ext
    if ext in DOCUMENT_EXTENSIONS:
        return "document", ext
    return "unknown", ext


def classify_url(value: str) -> tuple[ItemKind, str | None]:
    host = (urlsplit(value).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in EXPLORER_HOSTS):
        return "transaction", None
    kind, ext = classify_media(value)
    if kind in ("image", "document"):
        return kind, ext
    return "url", None


def extract(submission: RemoteSubmission) -> Extracted:
    text = strip_html(submission.content)
    raw_items: list[EvidenceItem] = []

    def strip_trailing(u: str) -> str:
        return u.rstrip(_TRAILING_PUNCT)

    for match in _URL_RE.findall(text):
        url = strip_trailing(match)
        kind, ext = classify_url(url)
        raw_items.append(EvidenceItem(url, normalize_url(url), kind, "content", ext))
    body = _URL_RE.sub(" ", text)

    for sig in _SIGNATURE_RE.findall(body):
        raw_items.append(EvidenceItem(sig, sig, "transaction", "content"))
    body = _SIGNATURE_RE.sub(" ", body)
    body = re.sub(r"\s+", " ", body).strip()

    unsupported: list[str] = []
    for media in submission.media:
        value = str(media).strip()
        if not value:
            continue
        if "://" in value:
            kind, ext = classify_url(value)
            normalized = normalize_url(value)
        else:
            kind, ext = classify_media(value)
            normalized = value
        if kind == "unknown":
            unsupported.append(value)
        raw_items.append(EvidenceItem(value, normalized, kind, "media", ext))

    seen: dict[str, int] = {}
    items: list[EvidenceItem] = []
    for item in raw_items:
        seen[item.normalized] = seen.get(item.normalized, 0) + 1
        if seen[item.normalized] == 1:
            items.append(item)
    duplicates = {k: n for k, n in seen.items() if n > 1}
    return Extracted(body=body, word_count=word_count(body), items=items, duplicates=duplicates, unsupported=unsupported)
