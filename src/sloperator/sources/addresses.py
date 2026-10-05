"""De-obfuscation of mailman/pipermail mangled addresses.

Pipermail archives rewrite ``@`` in headers as prose (``aconole at redhat.com``)
to discourage scrapers. Those headers must be restored before parsing, or every
author looks like an address-less localpart and principal matching silently
fails against the archive copy of a list.
"""

from __future__ import annotations

import re
from email.utils import parseaddr

_AT = re.compile(r"\s+at\s+|\[at\]|\(at\)", re.IGNORECASE)
_DOT = re.compile(r"\s+dot\s+|\[dot\]|\(dot\)", re.IGNORECASE)


def _is_plausible(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local and domain) and "." in domain


def deobfuscate_address(value: str | None) -> str | None:
    """Restore a mangled address header value, or return it unchanged.

    Values that already contain ``@`` are trusted as-is; anything else is only
    rewritten when the result parses into an address with a dotted domain.
    """
    if not value or "@" in value:
        return value
    text = _AT.sub("@", value)
    if "@" in text:
        text = _DOT.sub(".", text)
    _, email = parseaddr(text)
    if "@" not in email or not _is_plausible(email):
        return value
    return text
