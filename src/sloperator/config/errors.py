"""Error types for sloperator."""

from __future__ import annotations


class SloperatorError(Exception):
    """Base class for all sloperator errors."""


class ConfigError(SloperatorError):
    """Raised when configuration is missing, malformed, or invalid."""


class SecretError(SloperatorError):
    """Raised when a required secret cannot be resolved."""
