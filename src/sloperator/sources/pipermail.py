"""Pipermail/HyperKitty-style HTML archive source adapter.

Reads ``date.html`` for the ordered list of message pages (pipermail names
them 000000.html, 000001.html, ...), then fetches each new page and extracts
headers and body from the archive markup. The cursor is the last ingested
page filename, so polling only fetches what is newer.
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

_PAGE_LINK = re.compile(r"""["'](\d+\.html)["']""", re.IGNORECASE)
_HEADER_LABELS = {"from", "date", "message-id", "in-reply-to", "references"}
_DATE_FORMATS = (
    "%a %b %d %H:%M:%S %Z %Y",
    "%a %b %d %H:%M:%S %z %Y",
    "%a, %d %b %Y %H:%M:%S %z",
)


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

    from_text = parser.headers.get("from", "").strip()
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
    """Incremental reader for a pipermail-style HTML archive."""

    source_type = "pipermail"

    def __init__(self, config: PipermailSourceConfig, client: httpx.Client | None = None):
        self.source_id = config.id
        self._base_url = str(config.base_url).rstrip("/")
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _list_pages(self) -> list[str]:
        response = self._client.get(f"{self._base_url}/date.html")
        response.raise_for_status()
        pages = sorted(set(_PAGE_LINK.findall(response.text)))
        return pages

    def poll(self, cursor: str | None) -> PollResult:
        pages = self._list_pages()
        events: list[SourceEvent] = []
        new_cursor = cursor
        for page in pages:
            if cursor is not None and page <= cursor:
                continue
            response = self._client.get(f"{self._base_url}/{page}")
            response.raise_for_status()
            events.append(parse_page(response.text, self.source_id, page))
            new_cursor = page
        return PollResult(events=events, cursor=new_cursor)

    def health_check(self) -> SourceHealth:
        now = datetime.now(UTC)
        try:
            response = self._client.get(f"{self._base_url}/date.html")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DOWN,
                checked_at=now,
                detail=str(exc),
            )
        return SourceHealth(source_id=self.source_id, status=SourceHealthStatus.OK, checked_at=now)
