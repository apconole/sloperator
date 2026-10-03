"""Read-only git inspection tools.

All commands run the ``git`` binary with an argument list (never a shell) and
only ever execute read operations against the source repository. Anything
that could mutate authoritative state lives in the workspace module instead.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git invocation failed."""


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    author_name: str
    author_email: str
    date: str


_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_LOG_FORMAT = _FIELD_SEP.join(["%H", "%an", "%ae", "%aI", "%s"]) + _RECORD_SEP


class ReadOnlyGit:
    """Safe, read-only view over a local repository (bare or working tree)."""

    def __init__(self, repo_path: Path) -> None:
        if not repo_path.is_dir():
            raise GitError(f"repository path {repo_path} does not exist")
        self._path = repo_path

    @property
    def path(self) -> Path:
        return self._path

    def _run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self._path), *args],
            capture_output=True,
            text=True,
            env={"GIT_CONFIG_NOSYSTEM": "1", "PATH": _git_path()},
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        return proc.stdout

    def head(self, ref: str = "HEAD") -> str:
        return self._run("rev-parse", ref).strip()

    def branch_exists(self, ref: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self._path), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def log(self, ref: str = "HEAD", limit: int = 20) -> list[Commit]:
        raw = self._run("log", f"--max-count={limit}", f"--format={_LOG_FORMAT}", ref)
        commits: list[Commit] = []
        for record in raw.split(_RECORD_SEP):
            record = record.strip("\n")
            if not record.strip():
                continue
            fields = record.split(_FIELD_SEP)
            if len(fields) == 5:
                commits.append(
                    Commit(
                        sha=fields[0],
                        author_name=fields[1],
                        author_email=fields[2],
                        date=fields[3],
                        subject=fields[4],
                    )
                )
        return commits

    def show(self, rev: str) -> str:
        return self._run("show", "--format=fuller", rev)

    def show_file(self, rev: str, file_path: str) -> str:
        return self._run("show", f"{rev}:{file_path}")

    def diff(self, a: str, b: str | None = None, paths: list[str] | None = None) -> str:
        args = ["diff", a]
        if b is not None:
            args.append(b)
        if paths:
            args.extend(["--", *paths])
        return self._run(*args)

    def changed_files(self, a: str, b: str | None = None) -> list[str]:
        args = ["diff", "--name-only", a]
        if b is not None:
            args.append(b)
        out = self._run(*args)
        return [line for line in out.splitlines() if line]

    def ls(self, ref: str, directory: str = "") -> list[str]:
        out = self._run("ls-tree", "--name-only", f"{ref}:{directory}" if directory else ref)
        return [line for line in out.splitlines() if line]


def _git_path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")
