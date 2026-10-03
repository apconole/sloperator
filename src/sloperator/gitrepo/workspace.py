"""Isolated workspaces seeded from a source repository.

The application only ever reads from the source repository (a local clone
copies objects into the workspace). All agent-visible mutation happens
inside the workspace directory, with path containment enforced.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sloperator.gitrepo.tools import GitError, ReadOnlyGit


class WorkspaceError(RuntimeError):
    """Workspace creation or access failed."""


@dataclass(frozen=True)
class Workspace:
    name: str
    path: Path
    allow_modification: bool

    def git(self) -> ReadOnlyGit:
        return ReadOnlyGit(self.path)

    def _contained(self, relpath: str) -> Path:
        candidate = (self.path / relpath).resolve()
        if self.path not in candidate.parents and candidate != self.path:
            raise WorkspaceError(f"path {relpath!r} escapes workspace {self.name!r}")
        return candidate

    def read_file(self, relpath: str) -> str:
        path = self._contained(relpath)
        if not path.is_file():
            raise WorkspaceError(f"no such file in workspace: {relpath!r}")
        return path.read_text(encoding="utf-8")

    def write_file(self, relpath: str, content: str) -> Path:
        if not self.allow_modification:
            raise WorkspaceError(f"workspace {self.name!r} does not allow agent modification")
        path = self._contained(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def status(self) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git status failed: {proc.stderr.strip()[:200]}")
        return proc.stdout


class WorkspaceManager:
    """Creates and opens isolated workspaces below a root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def create(
        self,
        name: str,
        source_repo: Path,
        *,
        base_ref: str | None = None,
        allow_modification: bool = False,
    ) -> Workspace:
        workspace_path = (self._root / name).resolve()
        if self._root not in workspace_path.parents:
            raise WorkspaceError(f"workspace name {name!r} escapes root")
        if workspace_path.exists():
            raise WorkspaceError(f"workspace {name!r} already exists at {workspace_path}")
        if not source_repo.is_dir():
            raise WorkspaceError(f"source repository {source_repo} does not exist")
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--quiet",
                *(["--branch", base_ref] if base_ref else []),
                "--",
                str(source_repo),
                str(workspace_path),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise WorkspaceError(
                f"clone of {source_repo} failed ({proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        return Workspace(
            name=name,
            path=workspace_path,
            allow_modification=allow_modification,
        )

    def open(self, name: str, *, allow_modification: bool = False) -> Workspace:
        workspace_path = (self._root / name).resolve()
        if self._root not in workspace_path.parents:
            raise WorkspaceError(f"workspace name {name!r} escapes root")
        if not (workspace_path / ".git").is_dir():
            raise WorkspaceError(f"workspace {name!r} is not a git workspace")
        return Workspace(name=name, path=workspace_path, allow_modification=allow_modification)

    def remove(self, name: str) -> None:
        workspace = self.open(name)
        shutil.rmtree(workspace.path)
