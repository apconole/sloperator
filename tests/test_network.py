"""Tests for the network host allowlist."""

from __future__ import annotations

import pytest

from sloperator.security.network import host_of, is_host_allowed


def test_exact_match() -> None:
    assert is_host_allowed("github.com", ["github.com"])


def test_case_insensitive() -> None:
    assert is_host_allowed("GitHub.COM", ["github.com"])
    assert is_host_allowed("news.EXAMPLE.org", ["*.example.org"])


def test_no_match_denied_by_default() -> None:
    assert not is_host_allowed("evil.com", ["github.com"])
    assert not is_host_allowed("anything.com", [])


def test_wildcard_matches_subdomain() -> None:
    assert is_host_allowed("news.example.org", ["*.example.org"])


def test_wildcard_does_not_match_bare_domain() -> None:
    assert not is_host_allowed("example.org", ["*.example.org"])


def test_wildcard_cannot_be_spoofed_by_suffix() -> None:
    assert not is_host_allowed("a.example.org.evil.net", ["*.example.org"])


def test_exact_entry_does_not_match_subdomain() -> None:
    assert not is_host_allowed("news.example.org", ["example.org"])


def test_blank_entries_ignored() -> None:
    assert not is_host_allowed("example.org", ["", "  "])


def test_host_of_url() -> None:
    assert host_of("https://GitHub.com:443/x") == "github.com"
    assert host_of("http://flash.local:8080/v1") == "flash.local"


def test_host_of_rejects_non_url() -> None:
    with pytest.raises(ValueError, match="no hostname"):
        host_of("not-a-url")
