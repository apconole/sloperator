"""Tests for the local mbox source adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from sloperator.config.models import MboxSourceConfig
from sloperator.events import SourceHealthStatus
from sloperator.sources import PollResult
from sloperator.sources.mbox import MboxAdapter, parse_message


def _msg(
    msg_id: str,
    frm: str,
    subject: str,
    body: str,
    date: str = "Mon, 01 Oct 2026 12:00:00 +0000",
    extra: str = "",
) -> bytes:
    headers = f"Message-ID: <{msg_id}>\nFrom: {frm}\nTo: dev@example.org\n"
    headers += f"Subject: {subject}\nDate: {date}\n"
    headers += extra
    return (f"From bounce@example.org Mon Oct  1 12:00:00 2026\n{headers}\n{body}\n").encode()


@pytest.fixture()
def config(tmp_path: Path) -> MboxSourceConfig:
    return MboxSourceConfig(
        id="proj-mbox", project="proj", type="mbox", path=tmp_path / "list.mbox"
    )


def test_parse_message_extracts_headers_and_threading() -> None:
    raw = _msg(
        "abc@example.org",
        "Jane Doe <jane@example.org>",
        "hello",
        "body text",
        extra="References: <root@example.org>\nIn-Reply-To: <root@example.org>\n",
    )
    event = parse_message(raw, "proj-mbox")
    assert event.external_id == "abc@example.org"
    assert event.author is not None
    assert event.author.email == "jane@example.org"
    assert event.author.name == "Jane Doe"
    assert event.subject == "hello"
    assert event.reply_to == "root@example.org"
    assert event.references == ["root@example.org"]
    assert event.timestamp.year == 2026


def test_rfc2047_subject_is_decoded() -> None:
    raw = _msg("enc@example.org", "a@example.org", "=?utf-8?q?caf=C3=A9?=", "b")
    assert parse_message(raw, "proj-mbox").subject == "café"


def test_missing_message_id_falls_back_to_hash() -> None:
    raw = b"From x@e.org Mon Oct  1 12:00:00 2026\nFrom: a@example.org\nSubject: s\n\nb\n"
    event = parse_message(raw, "proj-mbox")
    assert event.external_id.startswith("sha256:")


def test_naive_date_is_treated_as_utc() -> None:
    raw = _msg("n@example.org", "a@example.org", "s", "b", date="Mon, 01 Oct 2026 09:00:00")
    event = parse_message(raw, "proj-mbox")
    assert event.timestamp.utcoffset() is not None


def test_poll_reads_all_then_only_appended(config: MboxSourceConfig) -> None:
    path = Path(config.path)
    path.write_bytes(_msg("m1@example.org", "a@example.org", "one", "b1"))

    adapter = MboxAdapter(config)
    first = adapter.poll(None)
    assert isinstance(first, PollResult)
    assert [e.external_id for e in first.events] == ["m1@example.org"]

    second = adapter.poll(first.cursor)
    assert second.events == []
    assert second.cursor == first.cursor

    with path.open("ab") as handle:
        handle.write(_msg("m2@example.org", "b@example.org", "two", "b2"))
    third = adapter.poll(second.cursor)
    assert [e.external_id for e in third.events] == ["m2@example.org"]
    assert int(third.cursor or "0") > int(first.cursor or "0")


def test_health_states(config: MboxSourceConfig, tmp_path: Path) -> None:
    adapter = MboxAdapter(config)
    assert adapter.health_check().status is SourceHealthStatus.DOWN

    Path(config.path).write_bytes(b"")
    assert adapter.health_check().status is SourceHealthStatus.DEGRADED

    Path(config.path).write_bytes(_msg("h@example.org", "a@example.org", "s", "b"))
    assert adapter.health_check().status is SourceHealthStatus.OK
