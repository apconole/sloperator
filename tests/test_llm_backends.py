"""Tests for the OpenAI-compatible backend (mock transport, no network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from sloperator.config.models import BackendConfig, RetryConfig
from sloperator.llm.backends import (
    BackendUnavailableError,
    LlmRequest,
    OpenAICompatibleBackend,
    StructuredOutputError,
)


class Verdict(BaseModel):
    approved: bool
    reasons: list[str] = Field(min_length=1)


def make_backend(
    handler: Callable[[httpx.Request], httpx.Response],
    **overrides: Any,
) -> tuple[OpenAICompatibleBackend, list[float]]:
    kwargs: dict[str, Any] = {
        "max_concurrency": 2,
        "retry": RetryConfig(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=8.0),
    }
    kwargs.update(overrides)
    config = BackendConfig(base_url="https://llm.example.test/v1", model="test-model", **kwargs)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=config.base_url.rstrip("/")
    )
    backend = OpenAICompatibleBackend("primary", config, client=client, sleep=fake_sleep)
    return backend, sleeps


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"model": "test-model", "choices": [{"message": {"content": content}}]}
    )


REQUEST = LlmRequest(system="sys", user="usr")


def test_complete_success_and_payload_shape() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return completion("hello there")

    async def run() -> None:
        backend, _ = make_backend(handler)
        response = await backend.complete(REQUEST)
        assert response.content == "hello there"
        assert response.backend == "primary"
        assert response.raw["model"] == "test-model"
        assert seen[0]["messages"][0] == {"role": "system", "content": "sys"}
        assert seen[0]["temperature"] == 0.2
        await backend.aclose()

    asyncio.run(run())


def test_retry_on_500_then_success_with_backoff() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="nope")
        return completion("ok")

    async def run() -> None:
        backend, sleeps = make_backend(handler)
        response = await backend.complete(REQUEST)
        assert response.content == "ok"
        assert calls["n"] == 3
        assert sleeps == [1.0, 2.0]
        await backend.aclose()

    asyncio.run(run())


def test_client_error_fails_fast() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    async def run() -> None:
        backend, sleeps = make_backend(handler)
        with pytest.raises(BackendUnavailableError, match="rejected request"):
            await backend.complete(REQUEST)
        assert calls["n"] == 1
        assert sleeps == []
        await backend.aclose()

    asyncio.run(run())


def test_exhausted_retries_raise_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def run() -> None:
        backend, sleeps = make_backend(handler)
        with pytest.raises(BackendUnavailableError, match="after 3 attempts"):
            await backend.complete(REQUEST)
        assert sleeps == [1.0, 2.0]
        await backend.aclose()

    asyncio.run(run())


def test_rate_limit_honors_retry_after() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
        return completion("fine")

    async def run() -> None:
        backend, sleeps = make_backend(handler)
        await backend.complete(REQUEST)
        assert 3.0 in sleeps
        await backend.aclose()

    asyncio.run(run())


def test_structured_output_validates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        return completion('{"approved": true, "reasons": ["lgtm"]}')

    async def run() -> None:
        backend, _ = make_backend(handler)
        verdict, response = await backend.complete_structured(REQUEST, Verdict)
        assert verdict.approved is True
        assert response.content.startswith("{")
        await backend.aclose()

    asyncio.run(run())


def test_structured_output_invalid_raises_with_raw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return completion("not json at all")

    async def run() -> None:
        backend, _ = make_backend(handler)
        with pytest.raises(StructuredOutputError) as excinfo:
            await backend.complete_structured(REQUEST, Verdict)
        assert excinfo.value.raw_content == "not json at all"
        await backend.aclose()

    asyncio.run(run())


def test_concurrency_bounded_by_semaphore() -> None:
    active = {"now": 0, "max": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        active["now"] -= 1
        return completion("x")

    async def run() -> None:
        backend, _ = make_backend(handler, max_concurrency=1)
        await asyncio.gather(*(backend.complete(REQUEST) for _ in range(5)))
        assert active["max"] == 1
        await backend.aclose()

    asyncio.run(run())
