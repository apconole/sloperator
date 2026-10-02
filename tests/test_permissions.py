"""Tests for tool permission declarations and runtime checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sloperator.security.errors import SecurityViolationError
from sloperator.security.paths import PathPolicy
from sloperator.security.permissions import (
    AccessLevel,
    ToolDeclaration,
    check_human_approval,
    check_tool_paths,
)


@pytest.fixture()
def roots(tmp_path: Path) -> dict[str, Path]:
    app = tmp_path / "app"
    repo = tmp_path / "repo.git"
    outside = tmp_path / "outside"
    for directory in (app, repo, outside):
        directory.mkdir()
    return {"app": app, "repo": repo, "outside": outside}


def make_policy(roots: dict[str, Path]) -> PathPolicy:
    return PathPolicy(read_roots=(roots["app"], roots["repo"]), write_roots=(roots["app"],))


def read_tool() -> ToolDeclaration:
    return ToolDeclaration(name="git.log", purpose="Show commit history.", access=AccessLevel.READ)


def write_tool() -> ToolDeclaration:
    return ToolDeclaration(
        name="workspace.write_file",
        purpose="Write a file inside an isolated workspace.",
        access=AccessLevel.WRITE,
    )


def test_read_tool_must_stay_in_read_roots(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    check_tool_paths(read_tool(), policy, [roots["repo"] / "foo.c"])
    with pytest.raises(SecurityViolationError):
        check_tool_paths(read_tool(), policy, [roots["outside"] / "id_rsa"])


def test_write_tool_must_stay_in_write_roots(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    check_tool_paths(write_tool(), policy, [roots["app"] / "proposed.patch"])
    with pytest.raises(SecurityViolationError):
        check_tool_paths(write_tool(), policy, [roots["repo"] / "HEAD"])


def test_human_approval_required() -> None:
    declaration = ToolDeclaration(
        name="external.send",
        purpose="Placeholder for future approved external actions.",
        access=AccessLevel.WRITE,
        requires_human_approval=True,
    )
    with pytest.raises(SecurityViolationError, match="requires explicit human approval"):
        check_human_approval(declaration, approved=False)
    check_human_approval(declaration, approved=True)


def test_declaration_requires_name_and_purpose() -> None:
    with pytest.raises(ValidationError):
        ToolDeclaration(name="", purpose="x", access=AccessLevel.READ)


def test_declaration_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ToolDeclaration(
            name="git.log",
            purpose="Show history.",
            access=AccessLevel.READ,
            shell=True,  # type: ignore[call-arg]
        )
