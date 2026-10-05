"""Local mbox source adapter.

Splits an mbox file on the unquoted ``From `` transport separator and parses
each message with the stdlib email machinery. The poll cursor is a byte
offset into the file, so repeated polling only reads newly appended messages
and re-ingestion is idempotent (external ids are Message-IDs).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from sloperator.config.models import MboxSourceConfig
from sloperator.events import Actor, SourceEvent, SourceHealth, SourceHealthStatus
from sloperator.sources import PollResult
from sloperator.sources.addresses import deobfuscate_address

_SEPARATOR = re.compile(rb"^From ", re.MULTILINE)


def _decode(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_date(raw: str | None) -> datetime:
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    return datetime.now(UTC)


def _body(msg: EmailMessage) -> str | None:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return None
        content = part.get_content()
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)
    except Exception:
        return None


def _clean_id(value: str) -> str:
    return value.strip().removeprefix("<").removesuffix(">")


def _raw_headers(msg: EmailMessage) -> dict[str, str]:
    """Header values exactly as written on the wire.

    Structured header parsing (``policy.default``) discards address comments,
    which is where pipermail keeps the display name of a mangled author, so
    identity headers are read from the raw values instead.
    """
    return {name.lower(): value for name, value in msg.raw_items()}


def _message_id(raw: bytes, headers: dict[str, str]) -> str:
    header = headers.get("message-id")
    if header:
        return _clean_id(header)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def parse_message(raw: bytes, source_id: str, source_type: str = "mbox") -> SourceEvent:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    headers = _raw_headers(msg)
    author_name, author_email = parseaddr(deobfuscate_address(headers.get("from")) or "")
    references = [_clean_id(ref) for ref in headers.get("references", "").split()]
    in_reply_to = headers.get("in-reply-to")
    reply_to = _clean_id(in_reply_to) if in_reply_to else None
    return SourceEvent(
        source_id=source_id,
        source_type=source_type,
        external_id=_message_id(raw, headers),
        timestamp=_parse_date(headers.get("date")),
        author=Actor(name=author_name or None, email=author_email or None),
        subject=_decode(msg.get("Subject")),
        body=_body(msg),
        references=references,
        reply_to=reply_to,
        metadata={"content_type": msg.get_content_type()},
    )


def split_messages(data: bytes, start: int = 0) -> Iterator[tuple[bytes, int]]:
    """Yield ``(raw_bytes, end_offset)`` for messages at or after ``start``.

    Messages are delimited by the unquoted ``From `` transport separator; the
    returned offset is the end of each message so callers can resume exactly
    where parsing stopped.
    """
    boundaries = [m.start() for m in _SEPARATOR.finditer(data)]
    for index, position in enumerate(boundaries):
        if position < start:
            continue
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(data)
        yield data[position:end], end


class MboxAdapter:
    """Reads a local mbox file incrementally by byte offset."""

    source_type = "mbox"

    def __init__(self, config: MboxSourceConfig) -> None:
        self.source_id = config.id
        self._path = Path(config.path)

    def poll(self, cursor: str | None) -> PollResult:
        data = self._path.read_bytes()
        start = int(cursor) if cursor else 0
        events: list[SourceEvent] = []
        new_cursor = start
        for chunk, end in split_messages(data, start):
            events.append(parse_message(chunk, self.source_id))
            new_cursor = end
        return PollResult(events=events, cursor=str(new_cursor))

    def health_check(self) -> SourceHealth:
        now = datetime.now(UTC)
        if not self._path.exists():
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DOWN,
                checked_at=now,
                detail=f"mbox file not found: {self._path}",
            )
        if not self._path.is_file() or not self._path.stat().st_size:
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DEGRADED,
                checked_at=now,
                detail="mbox file is empty or not a regular file",
            )
        return SourceHealth(
            source_id=self.source_id,
            status=SourceHealthStatus.OK,
            checked_at=now,
        )
