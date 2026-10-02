"""Network access control.

All outbound network access must be explicitly configured. Hostnames are
matched against a configured allowlist; anything not matched is denied.
The LLM never initiates network connections directly: only controlled
application tools consult this policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse


def host_of(url: str) -> str:
    """Return the lowercased hostname of an http(s) URL, without port."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"no hostname in URL: {url!r}")
    return parsed.hostname.lower()


def is_host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    """Return True if host matches an exact entry or a ``*.suffix`` wildcard.

    Matching is case-insensitive. A wildcard such as ``*.example.org``
    matches ``news.example.org`` but not the bare domain
    ``example.org`` and not ``example.org.evil.net``.
    """
    candidate = host.lower().strip()
    for entry in allowlist:
        normalized = entry.lower().strip()
        if not normalized:
            continue
        if normalized.startswith("*."):
            if candidate.endswith(normalized[1:]):
                return True
        elif candidate == normalized:
            return True
    return False
