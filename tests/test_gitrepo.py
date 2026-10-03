"""Tests for read-only git tools and isolated workspaces (real git repos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sloperator.gitrepo.tools import GitError, ReadOnlyGit
from sloperator.gitrepo.workspace import WorkspaceError, WorkspaceManager


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    (repo / "net.c").write_text("int main(void) { return 0; }\n")
    git("add", ".", cwd=repo)
    git("commit", "-m", "initial commit", cwd=repo)
    (repo / "net.c").write_text("int main(void) { return 1; }\n")
    git("commit", "-am", "change return value", cwd=repo)
    return repo


def test_head_log_and_show(source_repo: Path) -> None:
    g = ReadOnlyGit(source_repo)
    head = g.head()
    assert len(head) == 40
    commits = g.log(limit=10)
    assert [c.subject for c in commits] == ["change return value", "initial commit"]
    assert commits[0].author_email == "test@example.org"
    assert "change return value" in g.show(head)
    assert "return 1" in g.show_file(head, "net.c")
    assert "return 0" in g.show_file(commits[1].sha, "net.c")


def test_diff_and_changed_files(source_repo: Path) -> None:
    g = ReadOnlyGit(source_repo)
    first, second = (c.sha for c in reversed(g.log(limit=2)))
    diff = g.diff(first, second)
    assert "-int main(void) { return 0; }" in diff
    assert g.changed_files(first, second) == ["net.c"]


def test_branch_exists_and_errors(source_repo: Path) -> None:
    g = ReadOnlyGit(source_repo)
    assert g.branch_exists("main") is True
    assert g.branch_exists("nope") is False
    with pytest.raises(GitError):
        g.show_file("HEAD", "missing.c")
    with pytest.raises(GitError):
        ReadOnlyGit(source_repo / "nonexistent")


def test_ls_tree(source_repo: Path) -> None:
    g = ReadOnlyGit(source_repo)
    assert sorted(g.ls("HEAD")) == ["README.md", "net.c"]


def test_workspace_clone_read_and_modify(tmp_path: Path, source_repo: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create("task-1", source_repo, allow_modification=True)

    assert ws.read_file("net.c").strip().endswith("return 1; }")
    assert ws.git().head() == ReadOnlyGit(source_repo).head()

    ws.write_file("net.c", "int main(void) { return 2; }\n")
    assert " M net.c" in ws.status()

    reopened = manager.open("task-1", allow_modification=True)
    assert reopened.read_file("net.c").strip().endswith("return 2; }")


def test_workspace_modification_disabled(tmp_path: Path, source_repo: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create("task-2", source_repo)
    with pytest.raises(WorkspaceError, match="does not allow"):
        ws.write_file("extra.c", "x\n")


def test_workspace_path_containment(tmp_path: Path, source_repo: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create("task-3", source_repo, allow_modification=True)
    outside = tmp_path / "outside.txt"
    with pytest.raises(WorkspaceError, match="escapes workspace"):
        ws.write_file("../outside.txt", "evil")
    assert not outside.exists()
    with pytest.raises(WorkspaceError, match="escapes root"):
        manager.create("../escape", source_repo)


def test_workspace_source_untouched(tmp_path: Path, source_repo: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    before = sorted(p.name for p in source_repo.iterdir())
    ws = manager.create("task-4", source_repo, allow_modification=True)
    ws.write_file("brand-new.c", "new\n")
    after = sorted(p.name for p in source_repo.iterdir())
    assert before == after

    manager.remove("task-4")
    with pytest.raises(WorkspaceError, match="not a git workspace"):
        manager.open("task-4")
