"""Filesystem path policy.

The application may read only from explicitly configured roots and write
only into its own directories. All checks resolve the candidate path
(including symlinks and ``..`` segments) before matching, so a symlink
inside an allowed directory cannot smuggle access to a disallowed
location. Anything not explicitly allowed is denied.

This module is intentionally free of configuration imports; construct a
policy with PathPolicy.from_config at application startup.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sloperator.security.errors import SecurityViolationError

if TYPE_CHECKING:
    from sloperator.config.models import Config


@dataclass(frozen=True)
class PathPolicy:
    """Explicit read and write roots for all filesystem access."""

    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]

    @classmethod
    def from_config(cls, config: Config) -> PathPolicy:
        """Build a policy from loaded application configuration.

        Read roots: application directories, configured repositories,
        mbox source files, and security.extra_readable_paths. Write
        roots: application directories only. Authoritative repositories
        are never writable.
        """
        from sloperator.config.models import MboxSourceConfig

        app_dirs = (
            config.paths.database,
            config.paths.state,
            config.paths.cache,
            config.paths.artifacts,
            config.paths.workspaces,
            config.paths.logs,
        )
        read_roots: list[Path] = list(app_dirs)
        read_roots.extend(repo.path for repo in config.repositories.values())
        read_roots.extend(config.security.extra_readable_paths)
        read_roots.extend(
            source.path for source in config.sources if isinstance(source, MboxSourceConfig)
        )
        return cls(read_roots=tuple(read_roots), write_roots=app_dirs)

    def is_readable(self, path: Path) -> bool:
        """Return True if reading path is permitted by policy."""
        return self._under_any(path, self.read_roots)

    def is_writable(self, path: Path) -> bool:
        """Return True if writing path is permitted by policy."""
        return self._under_any(path, self.write_roots)

    def require_readable(self, path: Path) -> Path:
        """Return the resolved path if readable, else raise SecurityViolationError."""
        resolved = _resolve(path)
        if not _under_any(resolved, self.read_roots):
            raise SecurityViolationError(f"read access denied for {path}")
        return resolved

    def require_writable(self, path: Path) -> Path:
        """Return the resolved path if writable, else raise SecurityViolationError."""
        resolved = _resolve(path)
        if not _under_any(resolved, self.write_roots):
            raise SecurityViolationError(f"write access denied for {path}")
        return resolved

    def _under_any(self, path: Path, roots: Sequence[Path]) -> bool:
        return _under_any(_resolve(path), roots)


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _under_any(resolved: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        resolved_root = _resolve(root)
        if resolved == resolved_root or resolved.is_relative_to(resolved_root):
            return True
    return False
