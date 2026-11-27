"""Redaction must remove credentials without mangling ordinary text."""

from __future__ import annotations

import pytest

from openhands_connect.redaction import REDACTED, Redactor, redact


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abc123def456",
        "authorization=Bearer abc123def456",
        "X-Session-API-Key: 0123456789abcdef",
        '{"api_key": "sk-abcdefghijklmnop1234"}',
        "X-API-Key=topsecretvalue",
        "token: ghp_abcdefghijklmnopqrstuvwx",
    ],
)
def test_header_and_token_shapes_are_redacted(text: str) -> None:
    result = redact(text)
    assert REDACTED in result
    assert "abc123def456" not in result
    assert "0123456789abcdef" not in result
    assert "topsecretvalue" not in result
    assert "ghp_abcdefghijklmnopqrstuvwx" not in result


def test_literal_secret_is_removed_even_without_a_known_shape() -> None:
    secret = "zzz-not-a-known-token-shape-zzz"
    result = Redactor((secret,)).redact(f"failed to authenticate using {secret} against server")
    assert secret not in result
    assert REDACTED in result


def test_longest_literal_secret_wins() -> None:
    """A secret containing another secret must be fully removed."""
    inner, outer = "abcdef", "abcdef-extended"
    result = Redactor((inner, outer)).redact(f"value={outer}")
    assert inner not in result
    assert outer not in result


def test_ordinary_text_is_untouched() -> None:
    text = "Task finished successfully in 12.4 seconds; 3 files changed."
    assert redact(text) == text


def test_redaction_is_idempotent() -> None:
    once = redact("Authorization: Bearer sk-abcdefghijklmnop1234")
    assert redact(once) == once


def test_nested_mapping_values_are_redacted() -> None:
    result = Redactor().redact_mapping(
        {
            "headers": {"X-Session-API-Key": "0123456789abcdef"},
            "notes": ["Authorization: Bearer abc123def456"],
            "count": 3,
            "flag": True,
        }
    )
    headers = result["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Session-API-Key"] == REDACTED
    assert result["notes"] == [f"Authorization: Bearer {REDACTED}"]
    assert result["count"] == 3
    assert result["flag"] is True


def test_empty_input_is_safe() -> None:
    assert redact("") == ""
    assert Redactor().redact_mapping({"a": ""}) == {"a": ""}
