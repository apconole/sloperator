"""Tests for secret redaction helpers."""

from __future__ import annotations

from sloperator.security.redaction import REDACTED, redact_mapping, redact_text, register_secret


def test_registered_value_masked_in_text() -> None:
    register_secret("abc123")
    assert redact_text("token abc123 here") == f"token {REDACTED} here"


def test_unregistered_text_unchanged() -> None:
    assert redact_text("nothing sensitive here") == "nothing sensitive here"


def test_mapping_masks_sensitive_keys() -> None:
    result = redact_mapping({"api_key": "x", "note": "ok"})
    assert result["api_key"] == REDACTED
    assert result["note"] == "ok"


def test_nested_structures_masked() -> None:
    register_secret("topsecret")
    data = {
        "outer": {"password": "p", "text": "says topsecret"},
        "items": ["plain", "contains topsecret"],
    }
    result = redact_mapping(data)
    assert result["outer"]["password"] == REDACTED
    assert result["outer"]["text"] == f"says {REDACTED}"
    assert result["items"][0] == "plain"
    assert result["items"][1] == f"contains {REDACTED}"


def test_non_string_values_preserved() -> None:
    result = redact_mapping({"count": 3, "flag": True})
    assert result == {"count": 3, "flag": True}
