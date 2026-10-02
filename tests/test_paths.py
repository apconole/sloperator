"""Tests for the filesystem path policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from sloperator.config import load_config
from sloperator.security.errors import SecurityViolationError
from sloperator.security.paths import PathPolicy

FIXTURE = Path(__file__).parent / "fixtures" / "valid_config.yaml"


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


def test_read_inside_roots_allowed(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    assert policy.is_readable(roots["repo"] / "file.c")
    assert policy.is_readable(roots["app"] / "x")


def test_read_outside_denied(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    assert not policy.is_readable(roots["outside"] / "secret")


def test_write_limited_to_app_dirs(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    assert policy.is_writable(roots["app"] / "sub" / "f")
    assert not policy.is_writable(roots["repo"] / "f")
    assert not policy.is_writable(roots["outside"] / "f")


def test_require_readable_returns_resolved_path(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    target = roots["app"] / "sub"
    target.mkdir()
    resolved = policy.require_readable(target / ".." / "sub")
    assert resolved == target.resolve()


def test_require_readable_raises_for_outside(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    with pytest.raises(SecurityViolationError, match="read access denied"):
        policy.require_readable(roots["outside"] / "secret")


def test_require_writable_raises_for_repo(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    with pytest.raises(SecurityViolationError, match="write access denied"):
        policy.require_writable(roots["repo"] / "HEAD")


def test_symlink_escape_denied(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    (roots["app"] / "link").symlink_to(roots["outside"], target_is_directory=True)
    with pytest.raises(SecurityViolationError):
        policy.require_readable(roots["app"] / "link" / "secret.txt")


def test_parent_traversal_denied(roots: dict[str, Path]) -> None:
    policy = make_policy(roots)
    with pytest.raises(SecurityViolationError):
        policy.require_readable(roots["app"] / ".." / "outside" / "x")


def test_from_config_read_roots_include_repo_and_mbox() -> None:
    config = load_config(FIXTURE)
    policy = PathPolicy.from_config(config)
    assert policy.is_readable(Path("/srv/git/example.git"))
    assert policy.is_readable(Path("/archives/example.mbox"))
    assert not policy.is_readable(Path("/etc/passwd"))


def test_from_config_write_roots_are_app_dirs() -> None:
    config = load_config(FIXTURE)
    policy = PathPolicy.from_config(config)
    assert policy.is_writable(Path("/var/lib/sloperator/artifacts/report.md"))
    assert not policy.is_writable(Path("/srv/git/example.git"))
