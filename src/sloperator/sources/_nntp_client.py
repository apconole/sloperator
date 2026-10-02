"""Minimal NNTP client.

The stdlib ``nntplib`` was removed in Python 3.13 (PEP 594), so sloperator
ships the small subset of NNTP it needs: greeting, optional authinfo,
GROUP, ARTICLE, QUIT. Supports implicit TLS (e.g. port 563).
"""

from __future__ import annotations

import socket
import ssl
from collections import namedtuple
from email.message import Message
from email.parser import Parser

GroupInfo = namedtuple("GroupInfo", ["count", "first", "last"])
ArticleInfo = namedtuple("ArticleInfo", ["number", "message_id", "headers", "body"])


class NntpError(RuntimeError):
    """Protocol or connection failure."""


class ArticleMissingError(NntpError):
    """The requested article number is gone (423/430)."""


def unstuff(lines: list[str]) -> list[str]:
    """Undo NNTP byte-stuffing: a leading dot on a line is an escape."""
    return [line[1:] if line.startswith("..") else line for line in lines]


def parse_article_response(head: str, raw_lines: list[str]) -> ArticleInfo:
    """Parse a 220 response line plus the raw article text block."""
    parts = head.split()
    number: int | None = None
    head_message_id: str | None = None
    for token in parts[1:]:
        if token.startswith("<") and head_message_id is None:
            head_message_id = token
        bare = token.rstrip(":")
        if number is None and bare.isdigit():
            number = int(bare)

    headers: list[str] = []
    body: list[str] = []
    in_body = False
    for line in raw_lines:
        if not in_body and line == "":
            in_body = True
            continue
        (headers if not in_body else body).append(line)
    message: Message = Parser().parsestr("\n".join(headers))
    message_id = message.get("Message-ID") or head_message_id or ""
    return ArticleInfo(
        number=number,
        message_id=message_id.strip(),
        headers=message,
        body=unstuff(body),
    )


class SimpleNntpClient:
    """Blocking NNTP client covering GROUP/ARTICLE/QUIT."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        tls: bool = False,
        timeout: float = 30.0,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        sock = socket.create_connection((host, port), timeout=timeout)
        if tls:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        self._file = sock.makefile("rwb")
        code, _ = self._readline()
        if code not in (200, 201):
            raise NntpError(f"unexpected greeting: {code}")
        if username and password:
            self._command(f"AUTHINFO USER {username}", (381,))
            self._command(f"AUTHINFO PASS {password}", (281,))

    def _readline(self) -> tuple[int, str]:
        raw = self._file.readline()
        if not raw:
            raise NntpError("connection closed by server")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            return int(line[:3]), line
        except ValueError as exc:
            raise NntpError(f"malformed response: {line!r}") from exc

    def _command(self, command: str, expected: tuple[int, ...]) -> str:
        self._file.write(command.encode("utf-8") + b"\r\n")
        self._file.flush()
        code, line = self._readline()
        if code not in expected:
            if code in (423, 430):
                raise ArticleMissingError(line)
            raise NntpError(f"{command!r} failed: {line}")
        return line

    def group(self, group: str) -> GroupInfo:
        line = self._command(f"GROUP {group}", (211,))
        fields = line.split()
        if len(fields) < 4:
            raise NntpError(f"malformed GROUP response: {line!r}")
        return GroupInfo(count=int(fields[1]), first=int(fields[2]), last=int(fields[3]))

    def article(self, number: str) -> ArticleInfo:
        self._file.write(f"ARTICLE {number}\r\n".encode())
        self._file.flush()
        code, line = self._readline()
        if code in (423, 430):
            raise ArticleMissingError(line)
        if code != 220:
            raise NntpError(f"ARTICLE {number} failed: {line}")
        raw_lines: list[str] = []
        while True:
            raw = self._file.readline()
            if not raw:
                raise NntpError("connection closed mid-article")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text == ".":
                break
            raw_lines.append(text)
        return parse_article_response(line, raw_lines)

    def quit(self) -> None:
        try:
            self._command("QUIT", (205,))
        finally:
            self._file.close()
