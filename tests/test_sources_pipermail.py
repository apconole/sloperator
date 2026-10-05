"""Tests for the pipermail archive adapter (mocked transport)."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

import httpx
import pytest

from sloperator.config.models import PipermailSourceConfig
from sloperator.events import SourceHealthStatus
from sloperator.months import month_tag, parse_month_tag
from sloperator.sources.pipermail import PipermailAdapter, current_month_tag

BASE = "http://archive.example.org/pipermail/dev"

MONTH = current_month_tag(datetime(2026, 10, 5, tzinfo=UTC))


def _mbox_bytes(*ids: str) -> bytes:
    out = b""
    for msg_id in ids:
        headers = (
            f"Message-ID: <{msg_id}@dev.example.org>\n"
            f"From: aconole at redhat.com (Aaron Conole)\n"
            f"To: dev@example.org\n"
            f"Subject: [dev] patch {msg_id}\n"
            f"Date: Mon, 01 Oct 2026 12:00:00 +0000\n"
        )
        out += (
            f"From bounce@example.org Mon Oct  1 12:00:00 2026\n{headers}\nbody {msg_id}\n".encode()
        )
    return out


DATE_HTML = """<HTML><HEAD><TITLE>[dev] by date</TITLE></HEAD>
<BODY><H1>[dev] posts by date</H1>
<UL>
<LI><A HREF="000000.html">[dev] fix bar</A>
<LI><A HREF="000001.html">[dev] Re: fix bar</A>
</UL>
</BODY></HTML>"""

PAGE_0 = """<HTML><HEAD><TITLE>[dev] fix bar</TITLE></HEAD><BODY>
<H1>[dev] fix bar</H1>
<P>
<UL>
<LI><B>From:</B> <A HREF="mailto:jane@example.org">Jane Doe</A>
<LI><B>Date:</B> Thu Oct  1 12:00:00 UTC 2026
<LI><B>Message-ID:</B> &lt;root@dev.example.org&gt;
</UL>
<PRE>
Here is the fix.
</PRE>
</BODY></HTML>"""

PAGE_1 = """<HTML><HEAD><TITLE>[dev] Re: fix bar</TITLE></HEAD><BODY>
<H1>[dev] Re: fix bar</H1>
<P>
<UL>
<LI><B>From:</B> Bob &lt;bob@example.org&gt;
<LI><B>Date:</B> Thu Oct  1 14:30:00 UTC 2026
<LI><B>Message-ID:</B> &lt;reply@dev.example.org&gt;
<LI><B>In-Reply-To:</B> <A HREF="000000.html">&lt;root@dev.example.org&gt;</A>
</UL>
<PRE>
Looks good to me.
</PRE>
</BODY></HTML>"""


def make_client(files: dict[str, bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path not in files:
            return httpx.Response(404, text="missing")
        return httpx.Response(200, content=files[path])

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def config() -> PipermailSourceConfig:
    return PipermailSourceConfig(id="proj-web", project="proj", type="pipermail", base_url=BASE)


def test_poll_prefers_gzipped_mbox(config: PipermailSourceConfig) -> None:
    files = {f"/pipermail/dev/{MONTH}.txt.gz": gzip.compress(_mbox_bytes("m1", "m2"))}
    adapter = PipermailAdapter(config, client=make_client(files))
    result = adapter.poll(None)

    assert [e.external_id for e in result.events] == [
        "m1@dev.example.org",
        "m2@dev.example.org",
    ]
    author = result.events[0].author
    assert author is not None and author.email == "aconole@redhat.com"
    assert author.name == "Aaron Conole"
    assert result.cursor == f"{MONTH}@{len(_mbox_bytes('m1', 'm2'))}"


def test_poll_falls_back_to_plain_text_mbox(config: PipermailSourceConfig) -> None:
    files = {f"/pipermail/dev/{MONTH}.txt": _mbox_bytes("m1")}
    adapter = PipermailAdapter(config, client=make_client(files))
    result = adapter.poll(None)
    assert [e.external_id for e in result.events] == ["m1@dev.example.org"]


def test_mbox_poll_is_incremental_by_offset(config: PipermailSourceConfig) -> None:
    files = {f"/pipermail/dev/{MONTH}.txt": _mbox_bytes("m1")}
    adapter = PipermailAdapter(config, client=make_client(files))
    first = adapter.poll(None)
    assert len(first.events) == 1

    files[f"/pipermail/dev/{MONTH}.txt"] = _mbox_bytes("m1", "m2")
    second = adapter.poll(first.cursor)
    assert [e.external_id for e in second.events] == ["m2@dev.example.org"]
    assert adapter.poll(second.cursor).events == []


def test_poll_falls_back_to_html_without_mbox(config: PipermailSourceConfig) -> None:
    files = {
        f"/pipermail/dev/{MONTH}/date.html": DATE_HTML.encode(),
        f"/pipermail/dev/{MONTH}/000000.html": PAGE_0.encode(),
        f"/pipermail/dev/{MONTH}/000001.html": PAGE_1.encode(),
    }
    adapter = PipermailAdapter(config, client=make_client(files))
    result = adapter.poll(None)

    assert [e.external_id for e in result.events] == [
        "root@dev.example.org",
        "reply@dev.example.org",
    ]
    assert result.events[1].reply_to == "root@dev.example.org"
    assert result.cursor == f"{MONTH}@000001.html"

    files[f"/pipermail/dev/{MONTH}/000002.html"] = PAGE_1.replace(
        "reply@dev.example.org", "third@dev.example.org"
    ).replace('<A HREF="000000.html">', "")
    files[f"/pipermail/dev/{MONTH}/date.html"] = (
        DATE_HTML + '<A HREF="000002.html">[dev] third</A>'
    ).encode()
    third = adapter.poll(result.cursor)
    assert [e.external_id for e in third.events] == ["third@dev.example.org"]


def test_fetch_error_stops_before_cursor_advance(config: PipermailSourceConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".txt.gz"):
            return httpx.Response(404, text="missing")
        return httpx.Response(500, text="boom")

    adapter = PipermailAdapter(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError):
        adapter.poll(None)


def test_health_check(config: PipermailSourceConfig) -> None:
    files = {f"/pipermail/dev/{MONTH}.txt.gz": gzip.compress(_mbox_bytes("m1"))}
    assert PipermailAdapter(config, client=make_client(files)).health_check().status is (
        SourceHealthStatus.OK
    )

    html_only = {f"/pipermail/dev/{MONTH}/date.html": DATE_HTML.encode()}
    assert PipermailAdapter(config, client=make_client(html_only)).health_check().status is (
        SourceHealthStatus.OK
    )

    broken = PipermailAdapter(config, client=make_client({}))
    assert broken.health_check().status is SourceHealthStatus.DOWN


def _shift(months: int) -> str:
    now = datetime.now(UTC)
    index = now.year * 12 + (now.month - 1) - months
    return month_tag(index // 12, index % 12 + 1)


def _spec(months: int) -> str:
    year, month = parse_month_tag(_shift(months)) or (0, 0)
    return f"{year:04d}-{month:02d}"


def test_start_month_backfills_and_closes_past_months(config: PipermailSourceConfig) -> None:
    old, middle = _shift(2), _shift(1)
    files = {
        f"/pipermail/dev/{old}.txt": _mbox_bytes("o1"),
        f"/pipermail/dev/{middle}.txt": _mbox_bytes("m1"),
        f"/pipermail/dev/{MONTH}.txt": _mbox_bytes("c1"),
    }
    adapter = PipermailAdapter(
        config.model_copy(update={"start_month": _spec(2)}), client=make_client(files)
    )
    first = adapter.poll(None)
    assert [e.external_id for e in first.events] == [
        "o1@dev.example.org",
        "m1@dev.example.org",
        "c1@dev.example.org",
    ]
    assert first.cursor == f"{MONTH}@{len(files[f'/pipermail/dev/{MONTH}.txt'])}"

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path in files:
            return httpx.Response(200, content=files[request.url.path])
        return httpx.Response(404, text="missing")

    second = PipermailAdapter(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = second.poll(first.cursor)
    assert result.events == []
    assert requested == [f"/pipermail/dev/{MONTH}.txt.gz", f"/pipermail/dev/{MONTH}.txt"]


def test_missing_months_are_skipped(config: PipermailSourceConfig) -> None:
    files = {f"/pipermail/dev/{MONTH}.txt": _mbox_bytes("c1")}
    adapter = PipermailAdapter(
        config.model_copy(update={"start_month": _spec(1)}), client=make_client(files)
    )
    result = adapter.poll(None)
    assert [e.external_id for e in result.events] == ["c1@dev.example.org"]
