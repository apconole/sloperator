"""GitHub read-only source adapter.

Polls issues (which include pull requests in the GitHub API model) and
issue comments updated since the cursor timestamp. Strictly read-only:
no comments, reviews, or writes are ever attempted. The cursor is the
maximum ``updated_at`` seen, and listings are requested oldest-first so a
truncated page never loses items (the next poll resumes from it).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from sloperator.config.models import GithubSourceConfig
from sloperator.events import (
    Actor,
    EventType,
    SourceEvent,
    SourceHealth,
    SourceHealthStatus,
)
from sloperator.sources import PollResult

logger = logging.getLogger(__name__)


def _api_base(base_url: str) -> str:
    root = str(base_url).rstrip("/")
    if root in ("https://github.com", "http://github.com"):
        return "https://api.github.com"
    return f"{root}/api/v3"


def _parse_iso(value: str | None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return datetime.now(UTC)


class GithubAdapter:
    """Incremental, read-only reader for a GitHub repository."""

    source_type = "github"

    def __init__(
        self,
        config: GithubSourceConfig,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.source_id = config.id
        self._repository = config.repository
        self._api = _api_base(config.base_url)
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "sloperator"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.Client(base_url=self._api, headers=headers, timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, path: str, params: dict[str, str]) -> list[dict[str, object]]:
        response = self._client.get(
            f"/repos/{self._repository}{path}",
            params={**params, "per_page": "100"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError(f"unexpected non-list response for {path}")
        return data

    @staticmethod
    def _actor(user: object) -> Actor | None:
        if isinstance(user, dict) and user.get("login"):
            return Actor(name=str(user["login"]))
        return None

    @staticmethod
    def _text(item: dict[str, object], key: str) -> str | None:
        value = item.get(key)
        return value if isinstance(value, str) else None

    def _issue_event(self, item: dict[str, object]) -> SourceEvent:
        number = int(str(item["number"]))
        is_pr = "pull_request" in item
        return SourceEvent(
            source_id=self.source_id,
            source_type="github",
            external_id=f"issue-{number}",
            event_type=EventType.PR if is_pr else EventType.ISSUE,
            timestamp=_parse_iso(str(item.get("updated_at") or "")),
            author=self._actor(item.get("user")),
            subject=str(item.get("title") or ""),
            body=self._text(item, "body"),
            metadata={
                "number": number,
                "state": str(item.get("state") or ""),
                "html_url": str(item.get("html_url") or ""),
                "is_pull_request": is_pr,
            },
        )

    def _comment_event(self, item: dict[str, object]) -> SourceEvent:
        comment_id = int(str(item["id"]))
        return SourceEvent(
            source_id=self.source_id,
            source_type="github",
            external_id=f"comment-{comment_id}",
            event_type=EventType.ISSUE_COMMENT,
            timestamp=_parse_iso(str(item.get("updated_at") or item.get("created_at") or "")),
            author=self._actor(item.get("user")),
            body=self._text(item, "body"),
            metadata={
                "comment_id": comment_id,
                "html_url": str(item.get("html_url") or ""),
                "issue_url": self._text(item, "issue_url") or "",
            },
        )

    def poll(self, cursor: str | None) -> PollResult:
        params = {"state": "all", "sort": "updated", "direction": "asc"}
        if cursor:
            params["since"] = cursor

        events: list[SourceEvent] = []
        newest = cursor
        for item in self._get("/issues", dict(params)):
            events.append(self._issue_event(item))
            updated = str(item.get("updated_at") or "")
            if newest is None or updated > newest:
                newest = updated
        for item in self._get("/issues/comments", {"since": cursor} if cursor else {}):
            event = self._comment_event(item)
            events.append(event)
            stamp = event.timestamp.isoformat()
            if newest is None or stamp > newest:
                newest = stamp
        return PollResult(events=events, cursor=newest)

    def health_check(self) -> SourceHealth:
        now = datetime.now(UTC)
        try:
            response = self._client.get(f"/repos/{self._repository}")
        except httpx.HTTPError as exc:
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DOWN,
                checked_at=now,
                detail=str(exc),
            )
        if response.status_code == 200:
            return SourceHealth(
                source_id=self.source_id, status=SourceHealthStatus.OK, checked_at=now
            )
        if response.status_code in (401, 403):
            return SourceHealth(
                source_id=self.source_id,
                status=SourceHealthStatus.DEGRADED,
                checked_at=now,
                detail=f"auth problem: HTTP {response.status_code}",
            )
        return SourceHealth(
            source_id=self.source_id,
            status=SourceHealthStatus.DOWN,
            checked_at=now,
            detail=f"HTTP {response.status_code}",
        )
