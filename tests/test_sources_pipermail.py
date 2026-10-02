"""Tests for the pipermail HTML archive adapter (mocked transport)."""

from __future__ import annotations

import httpx
import pytest

from sloperator.config.models import PipermailSourceConfig
from sloperator.events import SourceHealthStatus
from sloperator.sources.pipermail import PipermailAdapter

BASE = "http://archive.example.org/pipermail/dev"

DATE_HTML = """<HTML><HEAD><TITLE>[dev] by date</TITLE></HEAD>
<BODY><H1>[dev] posts by date</H1>
<UL>
<LI><A HREF="000000.html">[dev] fix bar</A>
<LI><A HREF="000001.html">[dev] Re: fix bar</A>
</UL>
<A HREF="date.html">more...</A>
</BODY></HTML>"""

PAGE_0 = """<HTML><HEAD><TITLE>[dev] fix bar</TITLE></HEAD><BODY>
<H1>[dev] fix bar</H1>
<P>
<UL>
<LI><B>From:</B> <A HREF="mailto:jane@example.org">Jane Doe</A>
<LI><B>Date:</B> Thu Oct  1 12:00:00 UTC 2026
<LI><B>Message-ID:</B> &lt;root@dev.example.org&gt;
</UL>
<!-- message text -->
<PRE>
Here is the fix.

-
2.40.1
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
<!-- message text -->
<PRE>
Looks good to me.
</PRE>
</BODY></HTML>"""


def make_client(pages: dict[str, str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        page = pages.get(name)
        if page is None:
            return httpx.Response(404, text="missing")
        return httpx.Response(200, text=page)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def config() -> PipermailSourceConfig:
    return PipermailSourceConfig(id="proj-web", project="proj", type="pipermail", base_url=BASE)


@pytest.fixture()
def pages() -> dict[str, str]:
    return {"date.html": DATE_HTML, "000000.html": PAGE_0, "000001.html": PAGE_1}


def test_poll_extracts_threaded_events(
    config: PipermailSourceConfig, pages: dict[str, str]
) -> None:
    adapter = PipermailAdapter(config, client=make_client(pages))
    result = adapter.poll(None)
    assert [e.external_id for e in result.events] == [
        "root@dev.example.org",
        "reply@dev.example.org",
    ]
    first, second = result.events
    assert first.subject == "[dev] fix bar"
    assert first.author is not None and first.author.email == "jane@example.org"
    assert first.author.name == "Jane Doe"
    assert first.body is not None and "Here is the fix." in first.body
    assert second.reply_to == "root@dev.example.org"
    assert second.timestamp.year == 2026 and second.timestamp.utcoffset() is not None
    assert result.cursor == "000001.html"


def test_poll_is_incremental_by_page_name(
    config: PipermailSourceConfig, pages: dict[str, str]
) -> None:
    adapter = PipermailAdapter(config, client=make_client(pages))
    first = adapter.poll(None)
    assert adapter.poll(first.cursor).events == []

    pages["000002.html"] = PAGE_1.replace("reply@dev.example.org", "third@dev.example.org")
    pages["date.html"] = DATE_HTML + '<A HREF="000002.html">[dev] third</A>'
    third = adapter.poll(first.cursor)
    assert [e.external_id for e in third.events] == ["third@dev.example.org"]


def test_fetch_error_stops_before_cursor_advance(
    config: PipermailSourceConfig, pages: dict[str, str]
) -> None:
    del pages["000001.html"]
    adapter = PipermailAdapter(config, client=make_client(pages))
    with pytest.raises(httpx.HTTPStatusError):
        adapter.poll(None)


def test_health_check(config: PipermailSourceConfig, pages: dict[str, str]) -> None:
    adapter = PipermailAdapter(config, client=make_client(pages))
    assert adapter.health_check().status is SourceHealthStatus.OK

    broken = PipermailAdapter(config, client=make_client({}))
    assert broken.health_check().status is SourceHealthStatus.DOWN
