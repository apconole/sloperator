"""Pipermail-style mailing list archive source adapter.

Monthly mbox downloads (``YYYY-Month.txt.gz``) are the primary ingestion path
because they keep real headers; the HTML ``date.html`` listing of a month is
only used when no mbox download is published for it. The cursor is
``YYYY-Month@position``, where position is a byte offset into the mbox or the
last ingested HTML page name, so polling only reads what is newer.
"""

from __future__ import annotations

import calendar
import gzip
import html
import logging
import re
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

import httpx

from sloperator.config.models import PipermailSourceConfig
from sloperator.events import Actor, SourceEvent, SourceHealth, SourceHealthStatus
from sloperator.sources import PollResult
from sloperator.sources.addresses import deobfuscate_address
from sloperator.sources.mbox import parse_message, split_messages

logger = logging.getLogger(__name__)

_PAGE_LINK = re.compile(r"""["'](\d+\.html)["']""", re.IGNORECASE)
_HEADER_LABELS = {"from", "date", "message-id", "in-reply-to", "references"}
_DATE_FORMATS = (
    "%a %b %d %H:%M:%S %Z %Y",
    "%a %b %d %H:%M:%S %z %Y",
    "%a, %d %b %Y %H:%M:%S %z",
)


class _NoMbox(Exception):
    """Raised when a month has no published mbox download."""


def month_tag(year: int, month: int) -> str:
    """Archive directory/download name for a month, e.g. ``2026-October``."""
    return f"{year}-{calendar.month_name[month]}"


def current_month_tag(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return month_tag(now.year, now.month)


def _split_cursor(cursor: str) -> tuple[str, str]:
    tag, _, part = cursor.rpartition("@")
    return tag, part


def _cursor_part(cursor: str | None, tag: str) -> str | None:
    """Return the resume position for ``tag``, or None if the cursor is elsewhere."""
    if not cursor or "@" not in cursor:
        return None
    cursor_tag, part = _split_cursor(cursor)
    return part if cursor_tag == tag else None


class _PageParser(HTMLParser):
    """Stateful extraction of pipermail message pages (headers + <pre> body)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: dict[str, str] = {}
        self.subject: str | None = None
        self._body_parts: list[str] = []
        self._label: str | None = None
        self._bbuf: list[str] | None = None
        self._in_pre = False
        self._heading: list[str] | None = None
        self.mailto: str | None = None

    @property
    def body(self) -> str | None:
        text = "".join(self._body_parts)
        return text.strip("\n") or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "pre":
            self._in_pre = True
            self._label = None
        elif tag in ("h1", "h2"):
            self._heading = []
        elif tag == "b":
            self._bbuf = []
        elif tag == "a" and self._label == "from":
            for key, value in attrs:
                if key.lower() == "href" and value and value.lower().startswith("mailto:"):
                    self.mailto = value[len("mailto:") :].split("?", 1)[0] or None

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._in_pre = False
        elif tag in ("h1", "h2") and self._heading is not None:
            text = "".join(self._heading).strip()
            if text and self.subject is None:
                self.subject = text
            self._heading = None
        elif tag == "b" and self._bbuf is not None:
            label = "".join(self._bbuf).strip().rstrip(":").lower()
            self._label = label if label in _HEADER_LABELS else None
            self._bbuf = None

    def handle_data(self, data: str) -> None:
        if self._in_pre:
            self._body_parts.append(data)
        elif self._heading is not None:
            self._heading.append(data)
        elif self._bbuf is not None:
            self._bbuf.append(data)
        elif self._label is not None:
            self.headers[self._label] = self.headers.get(self._label, "") + data


def _parse_archive_date(raw: str | None) -> datetime:
    text = (raw or "").strip()
    if text:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return parsed.replace(tzinfo=parsed.tzinfo or UTC)
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is not None:
                return parsed
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


def _clean_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html.unescape(value).strip().removeprefix("<").removesuffix(">")
    return cleaned or None


def parse_page(markup: str, source_id: str, page: str) -> SourceEvent:
    parser = _PageParser()
    parser.feed(markup)

    from_text = deobfuscate_address(parser.headers.get("from", "").strip()) or ""
    name, email = parseaddr(from_text)
    if "@" not in email and parser.mailto:
        name = name or from_text
        email = parser.mailto
    references_text = parser.headers.get("references", "")
    references = [
        cleaned for ref in html.unescape(references_text).split() if (cleaned := _clean_id(ref))
    ]
    message_id = _clean_id(parser.headers.get("message-id")) or f"pipermail:{page}"

    return SourceEvent(
        source_id=source_id,
        source_type="pipermail",
        external_id=message_id,
        timestamp=_parse_archive_date(parser.headers.get("date")),
        author=Actor(name=name or None, email=email or None),
        subject=parser.subject,
        body=parser.body,
        references=references,
        reply_to=_clean_id(parser.headers.get("in-reply-to")),
        metadata={"content_type": "text/html", "archive_page": page},
    )


class PipermailAdapter:
    """Incremental reader for a pipermail-style archive.

    Monthly mbox downloads (``YYYY-Month.txt.gz``, falling back to
    ``YYYY-Month.txt``) are preferred because they carry real headers, so
    threading and authorship survive. HTML ``date.html`` listing pages are only
    used for months whose mbox download is not published.
    """

    source_type = "pipermail"

    def __init__(self, config: PipermailSourceConfig, client: httpx.Client | None = None):
        self.source_id = config.id
        self._base_url = str(config.base_url).rstrip("/")
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _month_url(self, name: str) -> str:
        return f"{self._base_url}/{name}"

    def _fetch_mbox(self, tag: str) -> bytes | None:
        """Return the monthly mbox body, or None when the archive has none."""
        for name in (f"{tag}.txt.gz", f"{tag}.txt"):
            response = self._client.get(self._month_url(name))
            if response.status_code == 404:
                continue
            response.raise_for_status()
            if name.endswith(".gz"):
                return gzip.decompress(response.content)
            return response.content
        return None

    def _mbox_month(self, tag: str, part: str | None, events: list[SourceEvent]) -> str:
        """Append messages newer than the offset in ``part``; return new part."""
        data = self._fetch_mbox(tag)
        if data is None:
            raise _NoMbox
        offset = int(part) if (part or "").isdigit() else 0
        for chunk, end in split_messages(data, offset):
            events.append(parse_message(chunk, self.source_id, self.source_type))
            offset = end
        return str(offset)

    def _list_pages(self, tag: str) -> list[str]:
        response = self._client.get(self._month_url(f"{tag}/date.html"))
        response.raise_for_status()
        return sorted(set(_PAGE_LINK.findall(response.text)))

    def _html_month(self, tag: str, part: str | None, events: list[SourceEvent]) -> str:
        """Fall back to HTML pages for a month without an mbox download."""
        last = part if part and not part.isdigit() else None
        for page in self._list_pages(tag):
            if last is not None and page <= last:
                continue
            response = self._client.get(self._month_url(f"{tag}/{page}"))
            response.raise_for_status()
            events.append(parse_page(response.text, self.source_id, page))
            last = page
        return last or ""

    def poll(self, cursor: str | None) -> PollResult:
        tag = current_month_tag()
        part = _cursor_part(cursor, tag)
        events: list[SourceEvent] = []
        try:
            new_part = self._mbox_month(tag, part, events)
        except _NoMbox:
            new_part = self._html_month(tag, part, events)
        return PollResult(events=events, cursor=f"{tag}@{new_part}")

    def health_check(self) -> SourceHealth:
        now = datetime.now(UTC)
        tag = current_month_tag()
        try:
            for name in (f"{tag}.txt.gz", f"{tag}.txt", f"{tag}/date.html"):
                response = self._client.get(self._month_url(name))
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                return SourceHealth(
                    source_id=self.source_id, status=SourceHealthStatus.OK, checked_at=now
                )
        except httpx.HTTPError as exc:
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DOWN,
                checked_at=now,
                detail=str(exc),
            )
        return SourceHealth(
            source_id=self.source_id,
            status=SourceHealthStatus.DOWN,
            checked_at=now,
            detail=f"no mbox or HTML archive found for {tag}",
        )
