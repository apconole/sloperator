"""Source adapters normalize external feeds into canonical SourceEvents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sloperator.events import SourceEvent, SourceHealth


@dataclass(frozen=True)
class PollResult:
    """One polling round: newly discovered events and the next cursor.

    Adapters must only advance the cursor past content that was fully
    parsed and returned, so a crash mid-poll re-reads (at worst) rather
    than loses messages.
    """

    events: list[SourceEvent] = field(default_factory=list)
    cursor: str | None = None


class SourceAdapter(Protocol):
    """Contract every source implementation fulfills."""

    source_id: str
    source_type: str

    def poll(self, cursor: str | None) -> PollResult:
        """Return events newer than cursor and the advanced cursor."""
        ...

    def health_check(self) -> SourceHealth:
        """Probe reachability/readability without ingesting."""
        ...


__all__ = ["PollResult", "SourceAdapter"]
