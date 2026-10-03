"""Tests for role-to-backend routing, overrides, and fallback."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from sloperator.config.loading import load_config
from sloperator.config.models import Config
from sloperator.llm.backends import (
    BackendUnavailableError,
    LlmRequest,
    LlmResponse,
    StructuredOutputError,
)
from sloperator.llm.routing import RoleRouter, UnknownRoleError

FIXTURE = Path(__file__).parent / "fixtures" / "valid_config.yaml"


class Verdict(BaseModel):
    approved: bool
    reasons: list[str] = Field(min_length=1)


class FakeBackend:
    def __init__(self, name: str, model: str = "m", fail: Exception | None = None) -> None:
        self.name = name
        self.model = model
        self.fail = fail
        self.calls: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        if self.fail is not None:
            raise self.fail
        return LlmResponse(content="ok", model=self.model, backend=self.name)

    async def complete_structured(
        self, request: LlmRequest, schema: type[BaseModel]
    ) -> tuple[Any, LlmResponse]:
        self.calls.append(request)
        if self.fail is not None:
            raise self.fail
        verdict = Verdict(approved=True, reasons=["fine"])
        return verdict, LlmResponse(content="{}", model=self.model, backend=self.name)


@pytest.fixture()
def config() -> Config:
    return load_config(FIXTURE)


def make_router(
    config: Config, backends: dict[str, FakeBackend]
) -> tuple[RoleRouter, dict[str, FakeBackend]]:
    router = RoleRouter(config, backends=backends)
    return router, backends


def all_fakes(**fails: Exception) -> dict[str, FakeBackend]:
    names = ["flash", "gpt-oss", "qwen27b"]
    return {n: FakeBackend(n, fail=fails.get(n)) for n in names}


def request() -> LlmRequest:
    return LlmRequest(system="s", user="u")


def test_resolution_precedence(config: Config) -> None:
    router, _ = make_router(config, all_fakes())
    assert router.resolve("triage").backend_name == "flash"
    assert router.resolve("conversation-analyst").backend_name == "gpt-oss"
    # project-level override wins for the example project
    route = router.resolve("conversation-analyst", project_id="example")
    assert route.backend_name == "qwen27b"
    assert route.fallback_name == "flash"


def test_global_routing_override(config: Config) -> None:
    updated = config.model_copy(
        update={
            "routing": config.routing.model_copy(update={"role_backends": {"triage": "qwen27b"}})
        }
    )
    router, _ = make_router(updated, all_fakes())
    assert router.resolve("triage").backend_name == "qwen27b"


def test_unknown_role_raises(config: Config) -> None:
    router, _ = make_router(config, all_fakes())
    with pytest.raises(UnknownRoleError):
        router.resolve("nonexistent")


def test_run_uses_resolved_backend(config: Config) -> None:
    router, fakes = make_router(config, all_fakes())

    async def run() -> None:
        response = await router.run("conversation-analyst", request(), project_id="example")
        assert response.backend == "qwen27b"
        assert fakes["qwen27b"].calls and not fakes["gpt-oss"].calls

    asyncio.run(run())


def test_fallback_on_unavailable(config: Config) -> None:
    router, fakes = make_router(config, all_fakes(**{"gpt-oss": BackendUnavailableError("down")}))

    async def run() -> None:
        response = await router.run("conversation-analyst", request())
        assert response.backend == "flash"
        assert len(fakes["gpt-oss"].calls) == 1
        assert len(fakes["flash"].calls) == 1

    asyncio.run(run())


def test_no_fallback_propagates(config: Config) -> None:
    router, _ = make_router(config, all_fakes(**{"flash": BackendUnavailableError("down")}))

    async def run() -> None:
        with pytest.raises(BackendUnavailableError):
            await router.run("triage", request())

    asyncio.run(run())


def test_structured_fallback_on_invalid_output(config: Config) -> None:
    router, fakes = make_router(
        config, all_fakes(**{"gpt-oss": StructuredOutputError("bad", "raw")})
    )

    async def run() -> None:
        verdict, response = await router.run_structured("conversation-analyst", request(), Verdict)
        assert verdict.approved is True
        assert response.backend == "flash"
        assert len(fakes["flash"].calls) == 1

    asyncio.run(run())


def test_role_model_and_temperature_overrides(config: Config) -> None:
    from sloperator.config.models import RoleConfig

    roles = dict(config.roles)
    roles["triage"] = RoleConfig(
        description="t", backend="flash", model="role-model", temperature=0.9
    )
    updated = config.model_copy(update={"roles": roles})
    router, fakes = make_router(updated, all_fakes())

    async def run() -> None:
        await router.run("triage", request())
        sent = fakes["flash"].calls[0]
        assert sent.model == "role-model"
        assert sent.temperature == 0.9

    asyncio.run(run())
