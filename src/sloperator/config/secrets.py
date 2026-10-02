"""Secret resolution.

Configuration files never contain secret values. They reference the name of an
environment variable instead. Secrets are resolved on demand; resolved values
must never be logged, serialized into artifacts, or persisted to the database.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from sloperator.config.errors import SecretError


class SecretStore:
    """Resolves named secrets from process environment variables."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def get(self, name: str) -> str:
        """Return the value of a required secret, or raise SecretError."""
        try:
            value = self._environ[name]
        except KeyError:
            raise SecretError(f"required environment variable {name!r} is not set") from None
        if not value:
            raise SecretError(f"environment variable {name!r} is set but empty")
        return value

    def get_optional(self, name: str | None) -> str | None:
        """Return the value of an optional secret; None passes through."""
        if name is None:
            return None
        return self.get(name)
