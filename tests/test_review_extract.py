from __future__ import annotations

from humanfallback.models import RemoteSubmission
from humanfallback.review import extract
from humanfallback.review.extract import classify_media, classify_url, normalize_url
from humanfallback.review.text import contract_terms, overlap, stem, strip_html, tokenize, word_count

SIG = "5VfYmGB7qXhQ" + "1" * 76  # 88 base58 characters


def _sub(content: str = "", media: list[str] | None = None) -> RemoteSubmission:
    return RemoteSubmission(id="s", task_id="t", status="pending", content=content, media=media or [])


class TestText:
    def test_strip_html(self) -> None:
        assert strip_html("<p>Hello &amp; <b>world</b></p>\n<br>") == "Hello & world"

    def test_tokenize_drops_stopwords_and_stems(self) -> None:
        assert tokenize("The photos were tagged and the shelves counted") == ["photo", "tag", "shelv", "count"]

    def test_stem(self) -> None:
        assert stem("stories") == "story"
        assert stem("tag") == "tag"
        assert stem("uses") == "us" or stem("uses") == "use"  # light stemming, not linguistic

    def test_word_count(self) -> None:
        assert word_count("") == 0
        assert word_count("  one   two three ") == 3

    def test_contract_terms_and_overlap(self) -> None:
        terms = contract_terms("Take a photo of the shelf", "Go to the hardware store and take a photo of the paint aisle.")
        assert "photo" in terms and "shelf" in terms and "hardware" in terms
        assert "the" not in terms
        matched, total, ratio = overlap("Here is the photo from the hardware store", terms)
        assert matched >= 2 and total == len(terms) and 0 < ratio <= 1
        assert overlap("completely unrelated prose", terms)[0] == 0
        assert overlap("anything", []) == (0, 0, 0.0)


class TestClassification:
    def test_media(self) -> None:
        assert classify_media("https://cdn/x.PNG") == ("image", "png")
        assert classify_media("report.pdf") == ("document", "pdf")
        assert classify_media("0a3e0b1c1111") == ("media", None)
        assert classify_media("thing.exe") == ("unknown", "exe")

    def test_url(self) -> None:
        assert classify_url("https://x.com/a/status/1") == ("url", None)
        assert classify_url("https://solscan.io/tx/abc") == ("transaction", None)
        assert classify_url("https://cdn.example.com/photo.jpg") == ("image", "jpg")

    def test_normalize_url(self) -> None:
        a = normalize_url("HTTPS://X.com/me/status/1/?utm_source=a&s=20#frag")
        b = normalize_url("https://x.com/me/status/1")
        assert a == b
        assert normalize_url("https://x.com/a?b=1&a=2") == normalize_url("https://x.com/a?a=2&b=1")
        assert normalize_url("https://x.com/a?id=1") != normalize_url("https://x.com/a?id=2")


class TestExtract:
    def test_urls_and_body(self) -> None:
        ex = extract(_sub("<p>Posted here: https://x.com/me/status/9. Thanks!</p>"))
        assert [i.value for i in ex.items] == ["https://x.com/me/status/9"]
        assert ex.body == "Posted here: Thanks!"
        assert ex.word_count == 3

    def test_media_and_transactions(self) -> None:
        ex = extract(_sub(f"tx {SIG} done", ["https://cdn/a.png", "mediaid", "bad.exe"]))
        kinds = {i.value: i.kind for i in ex.items}
        assert kinds[SIG] == "transaction"
        assert kinds["https://cdn/a.png"] == "image"
        assert kinds["mediaid"] == "media"
        assert kinds["bad.exe"] == "unknown"
        assert ex.unsupported == ["bad.exe"]
        assert ex.body == "tx done"

    def test_duplicates_collapsed(self) -> None:
        ex = extract(_sub("a https://x.com/p/1 b https://x.com/p/1?utm_source=z c", ["https://x.com/p/1/"]))
        assert len(ex.items) == 1
        assert ex.duplicates == {normalize_url("https://x.com/p/1"): 3}

    def test_empty(self) -> None:
        ex = extract(_sub(""))
        assert ex.items == [] and ex.word_count == 0 and ex.duplicates == {}
