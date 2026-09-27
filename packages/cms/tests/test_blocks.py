import pytest
from pydantic import ValidationError

from redblue.cms.blocks import blocks_text, validate_blocks
from redblue.cms.render import inline


def test_valid_blocks_roundtrip():
    out = validate_blocks(
        [
            {"type": "heading", "text": "Hello"},
            {"type": "paragraph", "text": "See [docs](/docs) and **bold**."},
            {"type": "faq", "items": [{"question": "Why?", "answer": "Because."}]},
            {"type": "image", "src": "/media/x.png", "alt": "A chart"},
        ]
    )
    assert out[0]["level"] == 2
    assert "Because." in blocks_text(out)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "paragraph", "text": "[x](javascript:alert(1))"},
        {"type": "cta", "heading": "h", "button_label": "b", "button_url": "javascript:x"},
        {"type": "image", "src": "/a.png"},  # no alt
        {"type": "embed", "provider": "youtube", "video_id": '"><script>', "title": "t"},
        {"type": "html", "html": "<script>alert(1)</script>"},
        {"type": "paragraph", "text": "ok", "onclick": "x"},
    ],
)
def test_unsafe_blocks_rejected(block):
    with pytest.raises(ValidationError):
        validate_blocks([block])


def test_decorative_image_allowed_without_alt():
    validate_blocks([{"type": "image", "src": "/a.png", "decorative": True}])


def test_inline_escapes_everything_else():
    out = str(inline('<script>alert(1)</script> **b** *i* `c<d>` [l](/x?a=1&b="2")'))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<strong>b</strong>" in out and "<em>i</em>" in out
    assert "<code>c&lt;d&gt;</code>" in out
    assert 'href="/x?a=1&amp;b=&#34;2&#34;"' in out
    assert "javascript" not in str(inline("[x](javascript:alert(1))"))
    assert 'rel="noopener"' in str(inline("[x](https://other.example)"))
