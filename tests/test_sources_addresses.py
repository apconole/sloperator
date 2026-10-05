"""Tests for pipermail address de-obfuscation."""

from __future__ import annotations

import pytest

from sloperator.sources.addresses import deobfuscate_address


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aconole at redhat.com (Aaron Conole)", "aconole@redhat.com (Aaron Conole)"),
        ("robot at bytheb.org (0-day Robot)", "robot@bytheb.org (0-day Robot)"),
        ("jane[at]example.org", "jane@example.org"),
        ("user at example dot com", "user@example.com"),
    ],
)
def test_restores_mangled_addresses(raw: str, expected: str) -> None:
    assert deobfuscate_address(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Jane Doe <jane@example.org>",
        "no address here",
        "dev-null at lists",
        "gates-at-microsoft.example.com",
        "",
        None,
    ],
)
def test_leaves_unmangleable_values_untouched(raw: str | None) -> None:
    assert deobfuscate_address(raw) == raw
