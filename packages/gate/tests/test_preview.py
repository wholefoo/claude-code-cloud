import pytest

from redblue.gate.config import parse_config
from redblue.gate.preview import NonLoopbackTarget, Preview, assert_loopback


@pytest.mark.parametrize("url", ["https://example.com", "http://10.0.0.1:8000",
                                 "http://localhost.evil.com"])
def test_non_loopback_refused(url):
    with pytest.raises(NonLoopbackTarget):
        assert_loopback(url)


def test_inprocess_preview_and_guard(hardened):
    cfg = parse_config({"app": "app:app"})
    with Preview(cfg, hardened) as p:
        assert p.client().get("/").status_code == 200
        assert p.openapi()["paths"]
        with pytest.raises(NonLoopbackTarget):
            p.client().get("https://example.com/")
