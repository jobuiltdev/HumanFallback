from __future__ import annotations

from humanfallback.models import RemoteSubmission
from humanfallback.review import extract
from humanfallback.review.extract import classify_media, classify_url, inline_images, normalize_url
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


# Sanitised copy of the HTML structure of a live stage submission
# (2026-09-15): the pasted screenshot arrives as an <img> inside
# the content while the media list is empty.
INLINE_IMG = "https://media.example.net/attachments/1111/2222/IMG_6608.jpg?ex=abc&is=def&hm=0123&=&format=webp&width=1200&height=600"
LIVE_INLINE_HTML = (
    "<p>Windows version: Microsoft Windows [Version 10.0.22631.4169]<br /><br /> uv run hf --version: humanfallback 0.4.0"
    "<br /><br />uv run hf classify \"Go to the hardware store and take a photo of the shelf\": <br />human_required: True "
    "<br />category: physical_action<br />confidence: 0.95 reasons: </p><ul><li><p>requires physical presence</p></li>"
    "<li><p>requires taking a photo of something real</p></li></ul>"
    f"<img src=\"{INLINE_IMG}\" alt=\"Image\" />"
    "<p><br /><br />Feedback: <br />The setup process was straightforward on Windows and the commands were easy to follow "
    "in the order provided. One improvement would be to make the prerequisites more explicit before the setup steps, "
    "especially that Git, Python, and uv should already be installed. It would also help new users if the instructions "
    "included a short example of the expected command output so they can confirm that everything is working.</p>"
)


class TestInlineImages:
    def test_live_html_yields_one_image_item(self) -> None:
        ex = extract(_sub(LIVE_INLINE_HTML))
        assert [(i.kind, i.source, i.extension) for i in ex.items] == [("image", "inline_image", "jpg")]
        assert ex.items[0].value == INLINE_IMG
        assert ex.items[0].normalized == normalize_url(INLINE_IMG)
        assert "IMG_6608" not in ex.body  # the tag is gone from the prose
        assert ex.word_count > 60
        assert ex.duplicates == {} and ex.unsupported == []

    def test_img_without_extension_is_still_an_image(self) -> None:
        ex = extract(_sub('<p>see</p><img src="https://cdn.example/media/abc123" />'))
        assert [(i.kind, i.extension) for i in ex.items] == [("image", None)]

    def test_single_quotes_entities_and_attribute_order(self) -> None:
        ex = extract(_sub("<IMG alt='x' SRC='https://cdn.example/a.png?a=1&amp;b=2'>"))
        assert ex.items[0].value == "https://cdn.example/a.png?a=1&b=2"
        assert ex.items[0].kind == "image"

    def test_data_uri_is_kept_but_truncated(self) -> None:
        payload = "data:image/png;base64," + "A" * 400
        ex = extract(_sub(f'<img src="{payload}">'))
        assert ex.items[0].kind == "image" and ex.items[0].extension == "png"
        assert ex.items[0].value.endswith("...") and len(ex.items[0].value) < 60
        assert ex.items[0].normalized == payload

    def test_empty_src_ignored(self) -> None:
        assert inline_images('<img src="" /><img alt="no src">') == []
        assert extract(_sub('<img src="">')).items == []

    def test_same_file_inline_and_in_media_is_one_artifact(self) -> None:
        ex = extract(_sub(f'<img src="{INLINE_IMG}">', [INLINE_IMG]))
        assert len(ex.items) == 1
        assert ex.items[0].source == "inline_image"
        assert ex.duplicates == {}  # not a repeated reference

    def test_separate_media_still_extracted(self) -> None:
        ex = extract(_sub(f'<img src="{INLINE_IMG}">', ["https://cdn.gib.work/media/other.png", "mediaid"]))
        assert [(i.kind, i.source) for i in ex.items] == [("image", "inline_image"), ("image", "media"), ("media", "media")]

    def test_img_url_repeated_as_plain_text_is_a_duplicate(self) -> None:
        ex = extract(_sub(f'<img src="{INLINE_IMG}"><p>also {INLINE_IMG}</p>'))
        assert len(ex.items) == 1
        assert ex.duplicates == {normalize_url(INLINE_IMG): 2}

    def test_prose_and_plain_urls_do_not_become_images(self) -> None:
        ex = extract(_sub("<p>screenshot attached, see https://x.com/me/status/9 and https://cdn.example/a.jpg</p>"))
        assert [(i.kind, i.source) for i in ex.items] == [("url", "content"), ("image", "content")]
        assert not [i for i in ex.items if i.source == "inline_image"]
