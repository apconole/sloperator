"""Typed configuration models for sloperator.

Configuration is validated in full at startup. Any invalid configuration
raises ConfigError with a precise message; the application never falls
back to unsafe defaults.

Secret values are never stored in configuration objects. Fields that refer to a
secret hold the name of an environment variable instead; values are resolved
on demand through SecretStore and must never be logged, serialized, or
persisted.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sloperator.months import parse_month_spec
from sloperator.security.network import host_of, is_host_allowed


class FrozenModel(BaseModel):
    """Immutable base model that rejects unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_http_url(value: str, field_name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute http(s) URL, got {value!r}")
    return value


def _require_absolute_path(value: Path, field_name: str) -> Path:
    if not value.is_absolute():
        raise ValueError(f"{field_name} must be an absolute path, got {str(value)!r}")
    return value


class Urgency(StrEnum):
    """Notification urgency levels in increasing order of attention."""

    INFO = "INFO"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"

    @property
    def severity(self) -> int:
        """Numeric rank used to filter notifications by minimum urgency."""
        order = {
            Urgency.INFO: 0,
            Urgency.ACTION_REQUIRED: 1,
            Urgency.HIGH_PRIORITY: 2,
            Urgency.READY_FOR_REVIEW: 3,
        }
        return order[self]


class ApplicationConfig(FrozenModel):
    """Identity of this sloperator instance."""

    name: str = "sloperator"


class PathsConfig(FrozenModel):
    """Writable application directories.

    All paths must be absolute and no directory may contain another, so
    that later security checks can reason about each area independently.
    """

    database: Path
    state: Path
    cache: Path
    artifacts: Path
    workspaces: Path
    logs: Path

    @model_validator(mode="after")
    def _check_paths(self) -> Self:
        entries: list[tuple[str, Path]] = [
            ("database", self.database),
            ("state", self.state),
            ("cache", self.cache),
            ("artifacts", self.artifacts),
            ("workspaces", self.workspaces),
            ("logs", self.logs),
        ]
        for name, path in entries:
            _require_absolute_path(path, f"paths.{name}")
        for (name_a, path_a), (name_b, path_b) in itertools.combinations(entries, 2):
            nested = path_a.is_relative_to(path_b) or path_b.is_relative_to(path_a)
            if path_a != path_b and nested:
                raise ValueError(f"paths.{name_a} and paths.{name_b} must not be nested")
        return self


class DatabaseConfig(FrozenModel):
    """Database settings. Only SQLite is supported in the initial release."""

    engine: Literal["sqlite"] = "sqlite"
    filename: str = Field(default="sloperator.db", min_length=1)
    busy_timeout_ms: int = Field(default=5000, ge=0)


class SourceConfig(FrozenModel):
    """Common fields for all source adapters."""

    id: str = Field(min_length=1)
    project: str = Field(min_length=1)


class MboxSourceConfig(SourceConfig):
    """Import messages from a local mbox file."""

    type: Literal["mbox"]
    path: Path
    store_raw: bool = True

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: Path) -> Path:
        return _require_absolute_path(value, "sources[].path")


class NntpSourceConfig(SourceConfig):
    """Read an NNTP-hosted mailing list."""

    type: Literal["nntp"]
    server: str = Field(min_length=1)
    port: int = Field(default=119, ge=1, le=65535)
    tls: bool = False
    group: str = Field(min_length=1)
    username: str | None = None
    password_env: str | None = None
    polling_interval_seconds: int = Field(default=60, ge=5)

    @field_validator("server")
    @classmethod
    def _check_server(cls, value: str) -> str:
        if any(bad in value for bad in (" ", "/", "@", ":")):
            raise ValueError(f"sources[].server must be a bare hostname, got {value!r}")
        return value


class PipermailSourceConfig(SourceConfig):
    """Read a Pipermail-style HTML/mbox archive.

    ``base_url`` points at the list archive root (the page listing months);
    adapters derive per-month paths from it. ``start_month`` (``YYYY-MM``)
    additionally backfills months from that month onward on first run.
    """

    type: Literal["pipermail"]
    base_url: str = Field(min_length=1)
    start_month: str | None = None
    polling_interval_seconds: int = Field(default=300, ge=30)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _require_http_url(value, "sources[].base_url")

    @field_validator("start_month")
    @classmethod
    def _check_start_month(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = parse_month_spec(value)
        if parsed is None:
            raise ValueError(f"sources[].start_month must be a YYYY-MM month, got {value!r}")
        year, month = parsed
        now = datetime.now(UTC)
        if year < 1995:
            raise ValueError(f"sources[].start_month is too old: {value!r}")
        if (year, month) > (now.year, now.month):
            raise ValueError(f"sources[].start_month must not be in the future: {value!r}")
        return f"{year:04d}-{month:02d}"


class GithubSourceConfig(SourceConfig):
    """Read a GitHub repository in read-only mode."""

    type: Literal["github"]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_url: str = "https://github.com"
    api_token_env: str | None = None
    polling_interval_seconds: int = Field(default=120, ge=10)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _require_http_url(value, "sources[].base_url")


AnySourceConfig = Annotated[
    MboxSourceConfig | NntpSourceConfig | PipermailSourceConfig | GithubSourceConfig,
    Field(discriminator="type"),
]


class ProjectConfig(FrozenModel):
    """A monitored project.

    ``roles`` maps a role name to a backend name and overrides both the
    global routing table and the role's own primary backend.
    """

    name: str = Field(min_length=1)
    description: str | None = None
    sources: list[str] = Field(default_factory=list)
    repository: str | None = None
    roles: dict[str, str] = Field(default_factory=dict)


class RepositoryConfig(FrozenModel):
    """A local git repository used for read-only investigation."""

    path: Path
    kind: Literal["bare", "working_tree"] = "working_tree"
    default_branch: str = Field(default="main", min_length=1)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: Path) -> Path:
        return _require_absolute_path(value, "repositories[].path")


class WorkspaceConfig(FrozenModel):
    """An isolated per-project workspace seeded from a source repository.

    Modifications inside a workspace never touch authoritative state; the
    application only ever reads from ``source_repo``.
    """

    source_repo: Path
    mode: Literal["isolated"] = "isolated"
    allow_agent_modification: bool = False

    @field_validator("source_repo")
    @classmethod
    def _check_source_repo(cls, value: Path) -> Path:
        return _require_absolute_path(value, "workspaces[].source_repo")


class PrincipalConfig(FrozenModel):
    """A maintainer identity whose activity and commitments are tracked."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RoleConfig(FrozenModel):
    """An LLM role with a primary backend and optional fallback."""

    description: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    fallback_backend: str | None = None
    model: str | None = Field(default=None, min_length=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class RetryConfig(FrozenModel):
    """Retry policy for backend requests."""

    max_attempts: int = Field(default=3, ge=1)
    base_delay_seconds: float = Field(default=1.0, gt=0)
    max_delay_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _check_delays(self) -> Self:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry.max_delay_seconds must be >= retry.base_delay_seconds")
        return self


class BackendConfig(FrozenModel):
    """An OpenAI-compatible chat completion endpoint."""

    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_context_tokens: int = Field(default=8192, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_concurrency: int = Field(default=1, ge=1)
    rate_limit_per_minute: int | None = Field(default=None, ge=1)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _require_http_url(value, "backends.*.base_url")


class RoutingConfig(FrozenModel):
    """Global role-to-backend overrides.

    Resolution order for the backend serving a role in a project:

    1. ``projects.<project>.roles[role]``
    2. ``routing.role_backends[role]``
    3. ``roles.<role>.backend``
    """

    role_backends: dict[str, str] = Field(default_factory=dict)


class SchedulingConfig(FrozenModel):
    """Persistent task queue and daemon loop settings."""

    daemon_poll_interval_seconds: int = Field(default=30, ge=5)
    max_concurrent_tasks: int = Field(default=4, ge=1)
    task_timeout_seconds: int = Field(default=600, ge=1)
    max_retries: int = Field(default=3, ge=0)
    backoff_base_seconds: float = Field(default=2.0, gt=0)
    backoff_max_seconds: float = Field(default=3600.0, gt=0)
    briefing_utc_hour: int = Field(default=8, ge=0, le=23)


class NotificationsConfig(FrozenModel):
    """Principal notification settings."""

    enabled: bool = True
    directory: Path | None = None
    min_urgency: Urgency = Urgency.INFO

    @field_validator("directory")
    @classmethod
    def _check_directory(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return _require_absolute_path(value, "notifications.directory")


class SecurityConfig(FrozenModel):
    """Explicit outbound network allowlist and extra read roots.

    An empty ``network_allowlist`` means no outbound network access at all.
    Entries are exact hostnames or ``*.suffix`` wildcards.
    """

    network_allowlist: list[str] = Field(default_factory=list)
    extra_readable_paths: list[Path] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_entries(self) -> Self:
        for entry in self.network_allowlist:
            host = entry[2:] if entry.startswith("*.") else entry
            if not host or any(bad in host for bad in (" ", "/", "@", ":")):
                raise ValueError(f"invalid network allowlist entry: {entry!r}")
        for path in self.extra_readable_paths:
            _require_absolute_path(path, "security.extra_readable_paths")
        return self


class LoggingConfig(FrozenModel):
    """Structured logging settings."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_format: bool = True


class Config(FrozenModel):
    """Top-level sloperator configuration.

    All cross-references between sections are validated at load time so
    that a malformed configuration fails clearly at startup instead of
    causing unsafe runtime behavior later.
    """

    application: ApplicationConfig
    paths: PathsConfig
    database: DatabaseConfig
    sources: list[AnySourceConfig]
    projects: dict[str, ProjectConfig]
    repositories: dict[str, RepositoryConfig]
    principals: list[PrincipalConfig]
    roles: dict[str, RoleConfig]
    backends: dict[str, BackendConfig]
    routing: RoutingConfig
    scheduling: SchedulingConfig
    notifications: NotificationsConfig
    security: SecurityConfig
    workspaces: dict[str, WorkspaceConfig]
    logging: LoggingConfig

    @model_validator(mode="after")
    def _cross_validate(self) -> Self:
        _validate_cross_references(self)
        return self


def _validate_cross_references(config: Config) -> None:
    allowlist = config.security.network_allowlist

    if not config.principals:
        raise ValueError("at least one principal must be configured")

    source_ids = [source.id for source in config.sources]
    duplicates = sorted({sid for sid in source_ids if source_ids.count(sid) > 1})
    if duplicates:
        raise ValueError(f"duplicate source ids: {', '.join(duplicates)}")

    for source in config.sources:
        if source.project not in config.projects:
            raise ValueError(f"source {source.id!r} references unknown project {source.project!r}")
        _check_source_network_allowed(source, allowlist)

    for backend_name, backend in config.backends.items():
        host = host_of(backend.base_url)
        if not is_host_allowed(host, allowlist):
            raise ValueError(
                f"backend {backend_name!r} connects to {host!r} which is not "
                "in security.network_allowlist"
            )

    for project_id, project in config.projects.items():
        for source_id in project.sources:
            if source_id not in source_ids:
                raise ValueError(f"project {project_id!r} references unknown source {source_id!r}")
        if project.repository is not None and project.repository not in config.repositories:
            raise ValueError(
                f"project {project_id!r} references unknown repository {project.repository!r}"
            )
        for role_name, backend_name in project.roles.items():
            if role_name not in config.roles:
                raise ValueError(f"project {project_id!r} overrides unknown role {role_name!r}")
            if backend_name not in config.backends:
                raise ValueError(
                    f"project {project_id!r} maps role {role_name!r} to "
                    f"unknown backend {backend_name!r}"
                )

    for role_name, role in config.roles.items():
        if role.backend not in config.backends:
            raise ValueError(f"role {role_name!r} references unknown backend {role.backend!r}")
        fallback = role.fallback_backend
        if fallback is not None:
            if fallback not in config.backends:
                raise ValueError(
                    f"role {role_name!r} references unknown fallback backend {fallback!r}"
                )
            if fallback == role.backend:
                raise ValueError(f"role {role_name!r} fallback backend must differ from primary")

    for role_name, backend_name in config.routing.role_backends.items():
        if role_name not in config.roles:
            raise ValueError(f"routing references unknown role {role_name!r}")
        if backend_name not in config.backends:
            raise ValueError(f"routing maps role {role_name!r} to unknown backend {backend_name!r}")

    for workspace_key in config.workspaces:
        if workspace_key not in config.projects:
            raise ValueError(f"workspace {workspace_key!r} has no matching project")


def _check_source_network_allowed(source: SourceConfig, allowlist: list[str]) -> None:
    hosts: list[str] = []
    if isinstance(source, NntpSourceConfig):
        hosts.append(source.server.lower())
    elif isinstance(source, PipermailSourceConfig | GithubSourceConfig):
        hosts.append(host_of(source.base_url))
    for host in hosts:
        if not is_host_allowed(host, allowlist):
            raise ValueError(
                f"source {source.id!r} connects to {host!r} which is not "
                "in security.network_allowlist"
            )
