"""Tests for the NNTP adapter using an injected fake client."""

from __future__ import annotations

import email
from collections.abc import Mapping

import pytest

from sloperator.config.models import NntpSourceConfig
from sloperator.events import SourceHealthStatus
from sloperator.sources._nntp_client import (
    ArticleInfo,
    ArticleMissingError,
    GroupInfo,
    parse_article_response,
    unstuff,
)
from sloperator.sources.nntp import NntpAdapter


def make_article(number: int, msg_id: str, **headers: str) -> ArticleInfo:
    text = "\n".join(f"{key}: {value}" for key, value in headers.items())
    return ArticleInfo(
        number=number,
        message_id=f"<{msg_id}>",
        headers=email.message_from_string(text),
        body=["line one", "line two"],
    )


class FakeNNTP:
    def __init__(self, first: int, last: int, articles: Mapping[int, ArticleInfo]):
        self.first, self.last, self.articles = first, last, dict(articles)
        self.quit_called = False

    def group(self, group: str) -> GroupInfo:
        return GroupInfo(count=self.last - self.first + 1, first=self.first, last=self.last)

    def article(self, number: str) -> ArticleInfo:
        key = int(number)
        if key not in self.articles:
            raise ArticleMissingError("423 no such article number in this group")
        return self.articles[key]

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture()
def config() -> NntpSourceConfig:
    return NntpSourceConfig(
        id="proj-nntp",
        project="proj",
        type="nntp",
        server="news.example.org",
        group="local.dev",
    )


@pytest.fixture()
def articles() -> dict[int, ArticleInfo]:
    return {
        1: make_article(
            1,
            "a1@news.example.org",
            From="Jane <jane@example.org>",
            Subject="first post",
            Date="Thu, 01 Oct 2026 10:00:00 +0000",
        ),
        2: make_article(
            2,
            "a2@news.example.org",
            From="Bob <bob@example.org>",
            Subject="=?utf-8?q?caf=C3=A9?=",
            Date="Thu, 01 Oct 2026 11:00:00 +0000",
            **{
                "In-Reply-To": "<a1@news.example.org>",
                "References": "<a1@news.example.org> <x@y>",
            },
        ),
        4: make_article(
            4,
            "a4@news.example.org",
            From="ann@example.org",
            Subject="fourth",
            Date="Thu, 01 Oct 2026 12:00:00 +0000",
        ),
    }


def test_backfill_skips_missing_articles(config: NntpSourceConfig, articles: dict) -> None:
    client = FakeNNTP(first=1, last=4, articles=articles)
    adapter = NntpAdapter(config, client_factory=lambda: client)
    result = adapter.poll(None)
    assert [e.external_id for e in result.events] == [
        "a1@news.example.org",
        "a2@news.example.org",
        "a4@news.example.org",
    ]
    assert result.cursor == "4"
    assert client.quit_called


def test_incremental_poll_by_article_number(config: NntpSourceConfig, articles: dict) -> None:
    client = FakeNNTP(first=1, last=4, articles=articles)
    adapter = NntpAdapter(config, client_factory=lambda: client)
    assert adapter.poll(None).cursor == "4"
    assert adapter.poll("4").events == []

    client.articles[5] = make_article(
        5,
        "a5@news.example.org",
        From="ann@example.org",
        Subject="fifth",
        Date="Thu, 01 Oct 2026 13:00:00 +0000",
    )
    client.last = 5
    result = adapter.poll("4")
    assert [e.external_id for e in result.events] == ["a5@news.example.org"]


def test_event_fields(config: NntpSourceConfig, articles: dict) -> None:
    client = FakeNNTP(first=1, last=4, articles=articles)
    adapter = NntpAdapter(config, client_factory=lambda: client)
    events = adapter.poll(None).events
    reply = events[1]
    assert reply.subject == "café"
    assert reply.reply_to == "a1@news.example.org"
    assert reply.references == ["a1@news.example.org", "x@y"]
    assert reply.body == "line one\nline two"
    assert reply.author is not None and reply.author.email == "bob@example.org"


def test_health_check(config: NntpSourceConfig, articles: dict) -> None:
    client = FakeNNTP(first=1, last=4, articles=articles)
    adapter = NntpAdapter(config, client_factory=lambda: client)
    health = adapter.health_check()
    assert health.status is SourceHealthStatus.OK
    assert health.detail is not None and "4" in health.detail

    def broken() -> FakeNNTP:
        raise OSError("connection refused")

    down = NntpAdapter(config, client_factory=broken).health_check()
    assert down.status is SourceHealthStatus.DOWN


def test_unstuff_removes_escape_dot() -> None:
    assert unstuff(["..From: x", "normal", "."]) == [".From: x", "normal", "."]


def test_parse_article_response_splits_headers_and_body() -> None:
    info = parse_article_response(
        "220 <art1@news.example.org> 42: article text follows",
        [
            "Message-ID: <art1@news.example.org>",
            "From: ann@example.org",
            "Subject: hi",
            "",
            "..quoted From line",
            "body",
        ],
    )
    assert info.number == 42
    assert info.message_id == "<art1@news.example.org>"
    assert info.headers["Subject"] == "hi"
    assert info.body == [".quoted From line", "body"]
