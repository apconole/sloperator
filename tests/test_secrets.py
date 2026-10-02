"""Tests for secret resolution."""

from __future__ import annotations

import pytest

from sloperator.config import SecretError, SecretStore


def test_get_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLOP_TEST_KEY", "s3cret")
    assert SecretStore().get("SLOP_TEST_KEY") == "s3cret"


def test_get_missing_raises() -> None:
    with pytest.raises(SecretError, match="not set"):
        SecretStore(environ={}).get("MISSING_VAR")


def test_get_empty_raises() -> None:
    with pytest.raises(SecretError, match="empty"):
        SecretStore(environ={"X": ""}).get("X")


def test_get_optional_none_passes_through() -> None:
    assert SecretStore(environ={}).get_optional(None) is None


def test_get_optional_resolves() -> None:
    assert SecretStore(environ={"X": "v"}).get_optional("X") == "v"


def test_error_message_does_not_leak_value() -> None:
    with pytest.raises(SecretError) as excinfo:
        SecretStore(environ={"X": ""}).get("X")
    assert "s3cret" not in str(excinfo.value)
