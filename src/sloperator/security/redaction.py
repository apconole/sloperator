"""Redaction of sensitive values from logs, audit entries, and artifacts.

Secret values resolved through SecretStore are registered here so that any
code path that formats text for output can mask them. Additionally, mapping
keys that look like credentials (``token``, ``password``, ...) are masked by
name regardless of value.

Redaction is defense in depth, not the primary boundary: secrets live only
in the environment and are never stored in configuration objects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"

_SENSITIVE_KEY = re.compile(r"(key|token|password|secret|credential|authorization)", re.IGNORECASE)

_registered_values: set[str] = set()


def register_secret(value: str) -> None:
    """Register a resolved secret value so formatted output masks it."""
    if value:
        _registered_values.add(value)


def reset_registered_secrets() -> None:
    """Forget all registered secrets. Intended for tests."""
    _registered_values.clear()


def redact_text(text: str) -> str:
    """Mask any registered secret values occurring in text."""
    result = text
    for value in _registered_values:
        if value and value in result:
            result = result.replace(value, REDACTED)
    return result


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of data with credential-looking keys and secret values masked."""
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY.search(str(key)):
            redacted[key] = REDACTED
        else:
            redacted[key] = _redact_value(value)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
