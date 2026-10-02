"""Tests for configuration loading, validation, and cross-reference checks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from sloperator.config import (
    Config,
    ConfigError,
    GithubSourceConfig,
    MboxSourceConfig,
    NntpSourceConfig,
    PipermailSourceConfig,
    Urgency,
    load_config,
    parse_config,
)

FIXTURE = Path(__file__).parent / "fixtures" / "valid_config.yaml"


def _data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return data


def _expect_error(mutate: Callable[[dict[str, Any]], None], *fragments: str) -> None:
    data = _data()
    mutate(data)
    with pytest.raises(ConfigError) as excinfo:
        parse_config(yaml.safe_dump(data))
    message = str(excinfo.value)
    for fragment in fragments:
        assert fragment in message, f"expected {fragment!r} in:\n{message}"


def test_valid_config_loads() -> None:
    config = load_config(FIXTURE)
    assert isinstance(config, Config)
    assert len(config.sources) == 4
    assert isinstance(config.sources[0], MboxSourceConfig)
    assert isinstance(config.sources[1], NntpSourceConfig)
    assert isinstance(config.sources[2], PipermailSourceConfig)
    assert isinstance(config.sources[3], GithubSourceConfig)
    assert config.backends["flash"].max_concurrency == 8
    assert config.projects["example"].roles == {"conversation-analyst": "qwen27b"}
    assert config.workspaces["example"].allow_agent_modification is True
    assert config.principals[0].email == "jane@example.org"
    assert config.scheduling.briefing_utc_hour == 8
    assert config.notifications.min_urgency is Urgency.INFO


def test_config_is_frozen() -> None:
    config = load_config(FIXTURE)
    with pytest.raises(ValidationError):
        config.paths.database = Path("/tmp/elsewhere")  # type: ignore[misc]


def test_missing_required_path_fails() -> None:
    _expect_error(lambda d: d["paths"].pop("state"), "paths.state", "Field required")


def test_relative_path_rejected() -> None:
    _expect_error(lambda d: d["paths"].__setitem__("cache", "cache"), "paths.cache")


def test_nested_paths_rejected() -> None:
    _expect_error(
        lambda d: d["paths"].__setitem__("state", "/var/lib/sloperator/database/state"),
        "must not be nested",
    )


def test_unknown_top_level_key_rejected() -> None:
    _expect_error(lambda d: d.__setitem__("bogus", 1), "bogus")


def test_unknown_source_type_rejected() -> None:
    _expect_error(
        lambda d: d["sources"][0].__setitem__("type", "rss"),
        "rss",
    )


def test_duplicate_source_ids_rejected() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["sources"][1]["id"] = data["sources"][0]["id"]

    _expect_error(mutate, "duplicate source ids")


def test_source_unknown_project_rejected() -> None:
    _expect_error(
        lambda d: d["sources"][0].__setitem__("project", "nope"),
        "unknown project",
    )


def test_project_unknown_source_rejected() -> None:
    _expect_error(
        lambda d: d["projects"]["example"]["sources"].append("ghost-source"),
        "unknown source",
    )


def test_project_unknown_repository_rejected() -> None:
    _expect_error(
        lambda d: d["projects"]["example"].__setitem__("repository", "ghost-repo"),
        "unknown repository",
    )


def test_role_unknown_backend_rejected() -> None:
    _expect_error(
        lambda d: d["roles"]["triage"].__setitem__("backend", "nope"),
        "unknown backend",
    )


def test_fallback_same_as_primary_rejected() -> None:
    _expect_error(
        lambda d: d["roles"]["conversation-analyst"].__setitem__("fallback_backend", "gpt-oss"),
        "must differ from primary",
    )


def test_project_role_override_unknown_role_rejected() -> None:
    _expect_error(
        lambda d: d["projects"]["example"]["roles"].__setitem__("ghost-role", "flash"),
        "unknown role",
    )


def test_routing_unknown_backend_rejected() -> None:
    _expect_error(
        lambda d: d["routing"].__setitem__("role_backends", {"triage": "nope"}),
        "unknown backend",
    )


def test_workspace_without_project_rejected() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["workspaces"]["ghost"] = {"source_repo": "/srv/git/ghost.git"}

    _expect_error(mutate, "no matching project")


def test_no_principals_rejected() -> None:
    _expect_error(lambda d: d.__setitem__("principals", []), "at least one principal")


def test_bad_principal_email_rejected() -> None:
    _expect_error(
        lambda d: d["principals"][0].__setitem__("email", "jane"),
        "principals.0.email",
    )


def test_nntp_server_not_in_allowlist_rejected() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["security"]["network_allowlist"] = [
            entry for entry in data["security"]["network_allowlist"] if entry != "news.example.org"
        ]

    _expect_error(mutate, "news.example.org", "network_allowlist")


def test_pipermail_host_not_in_allowlist_rejected() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["security"]["network_allowlist"] = [
            entry for entry in data["security"]["network_allowlist"] if entry != "lists.example.org"
        ]

    _expect_error(mutate, "lists.example.org")


def test_backend_host_not_in_allowlist_rejected() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["security"]["network_allowlist"] = [
            entry for entry in data["security"]["network_allowlist"] if entry != "*.local"
        ]

    _expect_error(mutate, "flash.local")


def test_invalid_allowlist_entry_rejected() -> None:
    _expect_error(
        lambda d: d["security"].__setitem__("network_allowlist", ["bad host"]),
        "invalid network allowlist entry",
    )


def test_github_repository_pattern_enforced() -> None:
    _expect_error(
        lambda d: d["sources"][3].__setitem__("repository", "no-slash"),
        "sources.3.github.repository",
    )


def test_invalid_yaml_rejected() -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        parse_config("\tbad: [\n  yaml: {")


def test_non_mapping_yaml_rejected() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        parse_config("- one\n- two\n")


def test_unreadable_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read configuration file"):
        load_config(tmp_path / "missing.yaml")


def test_multiple_field_errors_all_reported() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["principals"][0]["email"] = "not-an-email"
        data["sources"][3]["repository"] = "no-slash"

    _expect_error(mutate, "principals.0.email", "sources.3.github.repository")


def test_urgency_severity_ordering() -> None:
    severities = [
        Urgency.INFO.severity,
        Urgency.ACTION_REQUIRED.severity,
        Urgency.HIGH_PRIORITY.severity,
        Urgency.READY_FOR_REVIEW.severity,
    ]
    assert severities == sorted(severities)
    assert len(set(severities)) == 4
