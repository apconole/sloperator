"""Security-related error types."""

from __future__ import annotations

from sloperator.config.errors import SloperatorError


class SecurityViolationError(SloperatorError):
    """Raised when an operation would cross a security boundary.

    The system fails closed: any access that is not explicitly permitted
    by policy is denied.
    """
