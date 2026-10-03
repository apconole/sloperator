"""Tests for the read-only GitHub source adapter (mocked transport)."""

from __future__ import annotations

import httpx
import pytest

from sloperator.config.models import GithubSourceConfig
from sloperator.events import EventType, SourceHealthStatus
from sloperator.sources.github import GithubAdapter, _api_base

ISSUES = [
    {
        "number": 1,
        "title": "crash on startup",
        "body": "it crashes",
        "state": "open",
        "user": {"login": "reporter"},
        "created_at": "2026-10-01T09:00:00Z",
        "updated_at": "2026-10-01T09:00:00Z",
    },
    {
        "number": 2,
        "title": "add feature",
        "body": "please add",
        "state": "open",
        "user": {"login": "contrib"},
        "pull_request": {"url": "..."},
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z",
    },
]

COMMENTS = [
    {
        "id": 77,
        "body": "I can reproduce",
        "user": {"login": "helper"},
        "created_at": "2026-10-01T11:00:00Z",
        "updated_at": "2026-10-01T11:00:00Z",
        "html_url": "https://github.com/o/r/issues/1#issuecomment-77",
    }
]


def make_client(state: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        state.setdefault("requests", []).append(dict(request.url.params))
        if path.endswith("/repos/o/r/issues"):
            since = request.url.params.get("since")
            items = [i for i in state["issues"] if not since or i["updated_at"] > since]
            return httpx.Response(200, json=items)
        if path.endswith("/repos/o/r/issues/comments"):
            since = request.url.params.get("since")
            items = [c for c in state["comments"] if not since or c["updated_at"] > since]
            return httpx.Response(200, json=items)
        if path.endswith("/repos/o/r"):
            return httpx.Response(state.get("repo_status", 200), json={})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")


@pytest.fixture()
def config() -> GithubSourceConfig:
    return GithubSourceConfig(id="proj-gh", project="proj", type="github", repository="o/r")


@pytest.fixture()
def state() -> dict:
    return {"issues": ISSUES, "comments": COMMENTS}


def test_api_base_derivation() -> None:
    assert _api_base("https://github.com") == "https://api.github.com"
    assert _api_base("https://git.example.org/") == "https://git.example.org/api/v3"


def test_poll_maps_issues_prs_and_comments(config: GithubSourceConfig, state: dict) -> None:
    adapter = GithubAdapter(config, client=make_client(state))
    result = adapter.poll(None)
    by_id = {e.external_id: e for e in result.events}
    assert set(by_id) == {"issue-1", "issue-2", "comment-77"}
    assert by_id["issue-1"].event_type is EventType.ISSUE
    assert by_id["issue-2"].event_type is EventType.PR
    assert by_id["comment-77"].event_type is EventType.ISSUE_COMMENT
    assert by_id["issue-1"].author is not None
    assert by_id["issue-1"].author.key == "reporter"
    assert result.cursor == "2026-10-01T12:00:00Z"


def test_poll_passes_since_and_advances_cursor(config: GithubSourceConfig, state: dict) -> None:
    client = make_client(state)
    adapter = GithubAdapter(config, client=client)
    first = adapter.poll(None)
    assert adapter.poll(first.cursor).events == []

    state["issues"].append(
        {
            "number": 3,
            "title": "new",
            "body": "",
            "state": "open",
            "user": {"login": "u"},
            "created_at": "2026-10-02T08:00:00Z",
            "updated_at": "2026-10-02T08:00:00Z",
        }
    )
    second = adapter.poll(first.cursor)
    assert [e.external_id for e in second.events] == ["issue-3"]
    assert second.cursor == "2026-10-02T08:00:00Z"


def test_uses_asc_updated_ordering(config: GithubSourceConfig, state: dict) -> None:
    client = make_client(state)
    adapter = GithubAdapter(config, client=client)
    adapter.poll(None)
    issue_params = state["requests"][0]
    assert issue_params["sort"] == "updated"
    assert issue_params["direction"] == "asc"


def test_health_states(config: GithubSourceConfig, state: dict) -> None:
    adapter = GithubAdapter(config, client=make_client(state))
    assert adapter.health_check().status is SourceHealthStatus.OK

    state["repo_status"] = 403
    assert GithubAdapter(config, client=make_client(state)).health_check().status is (
        SourceHealthStatus.DEGRADED
    )

    state["repo_status"] = 404
    assert GithubAdapter(config, client=make_client(state)).health_check().status is (
        SourceHealthStatus.DOWN
    )
