"""Tool permission declarations and runtime authorization.

Every tool exposed to an LLM role must declare its name, purpose, and
access classification. The runtime enforces these declarations against
the path policy; prompt text such as "do not write" is never treated as
a security boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from sloperator.config.models import FrozenModel
from sloperator.security.errors import SecurityViolationError
from sloperator.security.paths import PathPolicy


class AccessLevel(StrEnum):
    """Whether a tool reads state or may modify files."""

    READ = "read"
    WRITE = "write"


class ToolDeclaration(FrozenModel):
    """Static security metadata for one tool.

    ``capabilities`` names the resource areas a tool may touch (for
    example ``git``, ``mail``, ``network:http``). Write access outside an
    isolated workspace always additionally requires human approval; no
    MVP-era tool is permitted to perform externally visible mutations.
    """

    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    access: AccessLevel
    capabilities: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False


def check_tool_paths(
    declaration: ToolDeclaration,
    policy: PathPolicy,
    paths: Iterable[Path],
) -> None:
    """Enforce that a tool call only touches locations its access level allows.

    A READ tool may touch nothing outside read roots; a WRITE tool may
    touch nothing outside write roots (which never include authoritative
    repositories). Raises SecurityViolationError on any violation.
    """
    for path in paths:
        if declaration.access is AccessLevel.READ:
            policy.require_readable(path)
        else:
            policy.require_writable(path)


def check_human_approval(declaration: ToolDeclaration, approved: bool) -> None:
    """Raise SecurityViolationError if a tool needs approval and none was given."""
    if declaration.requires_human_approval and not approved:
        raise SecurityViolationError(
            f"tool {declaration.name!r} requires explicit human approval; none was granted"
        )
