import pytest

from redblue.gate.config import ConfigError, parse_config
from redblue.gate.schemas import Severity


@pytest.mark.parametrize(
    "data",
    [
        {"target": "https://example.com"},
        {"url": "https://example.com"},
        {"target_url": "https://example.com"},
        {"scanners": {"dast": True}, "extra": {"base_url": "http://x"}},
    ],
)
def test_rejects_target_urls(data):
    with pytest.raises(ConfigError, match="preview"):
        parse_config(data)


def test_probe_paths_must_be_relative():
    with pytest.raises(ConfigError):
        parse_config({"probe_paths": ["https://evil.example/"]})
    assert parse_config({"probe_paths": ["/pricing"]}).probe_paths == ["/pricing"]


def test_ignore_needs_reason():
    with pytest.raises(ConfigError):
        parse_config({"ignore": [{"rule_id": "RB-DEBUG"}]})


def test_defaults():
    c = parse_config({})
    assert c.severity_threshold == Severity.high and c.app is None
