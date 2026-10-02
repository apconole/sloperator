"""Security boundaries: network access control, path policy, and audit."""

from sloperator.security.network import host_of, is_host_allowed

__all__ = ["host_of", "is_host_allowed"]
