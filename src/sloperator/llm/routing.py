"""Role-to-backend routing with project overrides and fallback.

Resolution order for the backend serving a role (per config contract):

1. ``projects.<project>.roles[role]``
2. ``routing.role_backends[role]``
3. ``roles.<role>.backend``

If the primary backend fails (unavailable or invalid structured output),
the request is retried once against ``roles.<role>.fallback_backend`` when
configured. Role-level model/temperature overrides are applied to every
request regardless of which backend serves it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from sloperator.config.models import Config
from sloperator.config.secrets import SecretStore
from sloperator.llm.backends import (
    BackendUnavailableError,
    LlmBackend,
    LlmRequest,
    LlmResponse,
    OpenAICompatibleBackend,
    StructuredOutputError,
)

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

_RETRYABLE = (BackendUnavailableError, StructuredOutputError)


class UnknownRoleError(KeyError):
    """A role was requested that configuration does not define."""


@dataclass(frozen=True)
class ResolvedRoute:
    role: str
    project_id: str | None
    backend_name: str
    fallback_name: str | None


class RoleRouter:
    """Resolves roles to backends and executes requests with fallback."""

    def __init__(
        self,
        config: Config,
        *,
        secret_store: SecretStore | None = None,
        backends: Mapping[str, LlmBackend] | None = None,
    ) -> None:
        self._config = config
        if backends is not None:
            self._backends: dict[str, LlmBackend] = dict(backends)
        else:
            store = secret_store or SecretStore()
            self._backends = {
                name: OpenAICompatibleBackend(
                    name, backend_cfg, api_key=store.get_optional(backend_cfg.api_key_env)
                )
                for name, backend_cfg in config.backends.items()
            }

    def resolve(self, role: str, project_id: str | None = None) -> ResolvedRoute:
        try:
            role_cfg = self._config.roles[role]
        except KeyError:
            raise UnknownRoleError(f"unknown role {role!r}") from None
        backend_name: str | None = None
        if project_id is not None:
            project = self._config.projects.get(project_id)
            if project is not None:
                backend_name = project.roles.get(role)
        if backend_name is None:
            backend_name = self._config.routing.role_backends.get(role)
        if backend_name is None:
            backend_name = role_cfg.backend
        return ResolvedRoute(
            role=role,
            project_id=project_id,
            backend_name=backend_name,
            fallback_name=role_cfg.fallback_backend,
        )

    def _backend(self, name: str) -> LlmBackend:
        try:
            return self._backends[name]
        except KeyError:
            raise UnknownRoleError(f"role references unknown backend {name!r}") from None

    def _apply_role_overrides(self, role: str, request: LlmRequest) -> LlmRequest:
        role_cfg = self._config.roles[role]
        if role_cfg.model is None and role_cfg.temperature is None:
            return request
        return LlmRequest(
            system=request.system,
            user=request.user,
            model=role_cfg.model or request.model,
            temperature=role_cfg.temperature
            if role_cfg.temperature is not None
            else request.temperature,
            max_output_tokens=request.max_output_tokens,
        )

    async def run(
        self, role: str, request: LlmRequest, project_id: str | None = None
    ) -> LlmResponse:
        route = self.resolve(role, project_id)
        prepared = self._apply_role_overrides(role, request)
        try:
            return await self._backend(route.backend_name).complete(prepared)
        except _RETRYABLE as exc:
            if route.fallback_name is None:
                raise
            logger.warning(
                "role %s primary backend %s failed (%s); using fallback %s",
                role,
                route.backend_name,
                type(exc).__name__,
                route.fallback_name,
            )
            return await self._backend(route.fallback_name).complete(prepared)

    async def run_structured(
        self,
        role: str,
        request: LlmRequest,
        schema: type[ModelT],
        project_id: str | None = None,
    ) -> tuple[ModelT, LlmResponse]:
        route = self.resolve(role, project_id)
        prepared = self._apply_role_overrides(role, request)
        try:
            return await self._backend(route.backend_name).complete_structured(prepared, schema)
        except _RETRYABLE as exc:
            if route.fallback_name is None:
                raise
            logger.warning(
                "role %s primary backend %s failed (%s); using fallback %s",
                role,
                route.backend_name,
                type(exc).__name__,
                route.fallback_name,
            )
            return await self._backend(route.fallback_name).complete_structured(prepared, schema)
