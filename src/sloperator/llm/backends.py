"""LLM access: OpenAI-compatible chat backends with retry and structured output."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from sloperator.config.models import BackendConfig

ModelT = TypeVar("ModelT", bound=BaseModel)


class LlmError(RuntimeError):
    """Base class for backend failures."""


class BackendUnavailableError(LlmError):
    """Backend kept failing after retries (or rejected the request hard)."""


class StructuredOutputError(LlmError):
    """The model answered but the payload did not validate; raw is kept."""

    def __init__(self, message: str, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content


@dataclass(frozen=True)
class LlmRequest:
    system: str
    user: str
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class LlmResponse:
    content: str
    model: str
    backend: str
    raw: dict[str, Any] = field(default_factory=dict)


class LlmBackend(Protocol):
    name: str
    model: str

    async def complete(self, request: LlmRequest) -> LlmResponse: ...

    async def complete_structured(
        self, request: LlmRequest, schema: type[ModelT]
    ) -> tuple[ModelT, LlmResponse]: ...


class OpenAICompatibleBackend:
    """Chat-completions client for OpenAI-style endpoints (incl. local ones).

    Retries transient failures (transport errors, 429, 5xx) with capped
    exponential backoff; 4xx rejections fail fast. A semaphore bounds
    concurrent requests per backend. JSON structured output is validated
    against the caller's pydantic model; invalid output raises
    StructuredOutputError carrying the raw text for audit/fallback.
    """

    def __init__(
        self,
        name: str,
        config: BackendConfig,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.model = config.model
        self._config = config
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            self._client = httpx.AsyncClient(
                base_url=config.base_url.rstrip("/"),
                headers=headers,
                timeout=config.timeout_seconds,
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _payload(self, request: LlmRequest, *, json_mode: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": request.temperature
            if request.temperature is not None
            else self._config.temperature,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        max_tokens = request.max_output_tokens or self._config.max_output_tokens
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        retry = self._config.retry
        last_error: str = "unknown error"
        async with self._semaphore:
            for attempt in range(1, retry.max_attempts + 1):
                try:
                    response = await self._client.post("/chat/completions", json=payload)
                except httpx.HTTPError as exc:
                    last_error = f"transport error: {exc}"
                else:
                    if response.status_code == 200:
                        data: dict[str, Any] = response.json()
                        return data
                    if response.status_code == 429:
                        last_error = "rate limited (429)"
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            with contextlib.suppress(ValueError):
                                await self._sleep(min(float(retry_after), retry.max_delay_seconds))
                    elif 500 <= response.status_code < 600:
                        last_error = f"server error {response.status_code}"
                    else:
                        raise BackendUnavailableError(
                            f"backend {self.name!r} rejected request "
                            f"({response.status_code}): {response.text[:200]}"
                        )
                if attempt < retry.max_attempts:
                    delay = min(
                        retry.base_delay_seconds * (2 ** (attempt - 1)),
                        retry.max_delay_seconds,
                    )
                    await self._sleep(delay)
        raise BackendUnavailableError(
            f"backend {self.name!r} failed after {retry.max_attempts} attempts: {last_error}"
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        data = await self._post(self._payload(request, json_mode=False))
        return LlmResponse(
            content=self._extract_content(data),
            model=str(data.get("model", self.model)),
            backend=self.name,
            raw=data,
        )

    async def complete_structured(
        self, request: LlmRequest, schema: type[ModelT]
    ) -> tuple[ModelT, LlmResponse]:
        data = await self._post(self._payload(request, json_mode=True))
        response = LlmResponse(
            content=self._extract_content(data),
            model=str(data.get("model", self.model)),
            backend=self.name,
            raw=data,
        )
        try:
            parsed = json.loads(response.content)
            return schema.model_validate(parsed), response
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StructuredOutputError(
                f"backend {self.name!r} returned invalid structured output: {exc}",
                response.content,
            ) from exc

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"malformed completion response: {data}") from exc
        message: str = content or ""
        return message
