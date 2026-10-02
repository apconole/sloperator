"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sloperator.security.redaction import reset_registered_secrets


@pytest.fixture(autouse=True)
def clean_secret_registry() -> Iterator[None]:
    """Keep the process-wide secret registry isolated between tests."""
    reset_registered_secrets()
    yield
    reset_registered_secrets()
