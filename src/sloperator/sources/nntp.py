"""NNTP (network news) source adapter for NNTP-hosted mailing lists.

Articles are polled by article number within the group; the cursor is the
last fetched article number. Deleted articles are skipped without stalling
the cursor. Connections are opened per poll, which keeps the state machine
simple and crash-safe. TLS and username/password auth are supported; the
password is injected by the caller (resolved from an env-var secret), never
stored in config.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Protocol

from sloperator.config.models import NntpSourceConfig
from sloperator.events import Actor, SourceEvent, SourceHealth, SourceHealthStatus
from sloperator.sources import PollResult
from sloperator.sources._nntp_client import (
    ArticleInfo,
    ArticleMissingError,
    GroupInfo,
    NntpError,
    SimpleNntpClient,
)

logger = logging.getLogger(__name__)


class NntpClient(Protocol):
    """Subset of an NNTP connection used by the adapter (allows injection)."""

    def group(self, group: str) -> GroupInfo: ...

    def article(self, number: str) -> ArticleInfo: ...

    def quit(self) -> None: ...


def _decode(value: str | None) -> str | None:
    if value is None:
        return None
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _clean_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().removeprefix("<").removesuffix(">")
    return cleaned or None


class NntpAdapter:
    """Incremental reader for an NNTP newsgroup."""

    source_type = "nntp"

    def __init__(
        self,
        config: NntpSourceConfig,
        *,
        password: str | None = None,
        client_factory: Callable[[], NntpClient] | None = None,
    ) -> None:
        self.source_id = config.id
        self._config = config
        self._password = password
        self._client_factory: Callable[[], NntpClient] = client_factory or self._connect

    def _connect(self) -> SimpleNntpClient:
        return SimpleNntpClient(
            self._config.server,
            self._config.port,
            tls=self._config.tls,
            username=self._config.username,
            password=self._password,
        )

    def _to_event(self, number: int, info: ArticleInfo) -> SourceEvent:
        headers = info.headers
        name, email = parseaddr(headers.get("From") or "")
        references = [
            cleaned
            for ref in (headers.get("References") or "").split()
            if (cleaned := _clean_id(ref))
        ]
        message_id = _clean_id(info.message_id) or f"nntp:{number}"

        timestamp: datetime | None = None
        date_raw = headers.get("Date")
        if date_raw:
            try:
                timestamp = parsedate_to_datetime(date_raw)
            except (TypeError, ValueError):
                timestamp = None
        if timestamp is None:
            timestamp = datetime.now(UTC)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        return SourceEvent(
            source_id=self.source_id,
            source_type="nntp",
            external_id=message_id,
            timestamp=timestamp,
            author=Actor(name=name or None, email=email or None),
            subject=_decode(headers.get("Subject")),
            body="\n".join(info.body) if info.body else None,
            references=references,
            reply_to=_clean_id(headers.get("In-Reply-To")),
            metadata={"content_type": "text/plain", "article_number": number},
        )

    def poll(self, cursor: str | None) -> PollResult:
        client = self._client_factory()
        try:
            info = client.group(self._config.group)
            first, last = int(info.first), int(info.last)
            start = first if cursor is None else max(int(cursor) + 1, first)
            events: list[SourceEvent] = []
            new_cursor = cursor
            for number in range(start, last + 1):
                try:
                    article = client.article(str(number))
                except ArticleMissingError:
                    logger.warning(
                        "skipping missing article",
                        extra={"source_id": self.source_id, "article": number},
                    )
                    new_cursor = str(number)
                    continue
                events.append(self._to_event(number, article))
                new_cursor = str(number)
            return PollResult(events=events, cursor=new_cursor)
        finally:
            with contextlib.suppress(Exception):
                client.quit()

    def health_check(self) -> SourceHealth:
        now = datetime.now(UTC)
        try:
            client = self._client_factory()
            info = client.group(self._config.group)
            client.quit()
        except (OSError, NntpError) as exc:
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DOWN,
                checked_at=now,
                detail=str(exc),
            )
        return SourceHealth(
            source_id=self.source_id,
            status=SourceHealthStatus.OK,
            checked_at=now,
            detail=f"group has {info.count} articles ({info.first}-{info.last})",
        )
