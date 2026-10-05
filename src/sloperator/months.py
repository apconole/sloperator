"""Month arithmetic for monthly-partitioned archive sources.

Pipermail names its month directories and mbox downloads after the full month
name (``2026-October``), while configuration spells months as ``YYYY-MM``. This
module is the single place that converts between the two.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime

_MONTH_SPEC = re.compile(r"^(\d{4})-(\d{1,2})$")
_MONTH_TAG = re.compile(r"^(\d{4})-([A-Za-z]+)$")
_MONTH_NAMES = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}


def month_tag(year: int, month: int) -> str:
    """Archive name for a month, e.g. ``2026-October``."""
    return f"{year}-{calendar.month_name[month]}"


def current_month_tag(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return month_tag(now.year, now.month)


def parse_month_spec(value: str) -> tuple[int, int] | None:
    """Parse a ``YYYY-MM`` configuration value into ``(year, month)``."""
    match = _MONTH_SPEC.match(value.strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month


def parse_month_tag(value: str) -> tuple[int, int] | None:
    """Parse an archive month name such as ``2026-October``."""
    match = _MONTH_TAG.match(value.strip())
    if match is None:
        return None
    month = _MONTH_NAMES.get(match.group(2).lower())
    return (int(match.group(1)), month) if month else None


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def month_range(
    start: tuple[int, int], end: tuple[int, int] | None = None
) -> list[tuple[int, int]]:
    """Inclusive list of months from ``start`` to ``end`` (default: now)."""
    end_year, end_month = end or _now_ym()
    months: list[tuple[int, int]] = []
    current = start
    while current <= (end_year, end_month) and len(months) < 1200:
        months.append(current)
        current = next_month(*current)
    return months


def _now_ym() -> tuple[int, int]:
    now = datetime.now(UTC)
    return now.year, now.month
