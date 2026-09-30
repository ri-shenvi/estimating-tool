from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bidtriage.ingestion.eml import ParsedMessage


@dataclass
class PollResult:
    messages: list[tuple[str, ParsedMessage]]  # (provider_message_id, parsed)
    new_state: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    more_available: bool = False
    """True when the source stopped at `limit` and has further messages ready right now."""


@runtime_checkable
class MailSource(Protocol):
    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult: ...


@runtime_checkable
class BackfillableSource(Protocol):
    """A source that can walk history oldest-first for the first-connection backfill (SPEC-01 F7).

    `seed` returns a state whose live cursor sits at *now*, so the first live poll does not drain
    the whole folder and history is covered by `backfill` within the configured window.
    """

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult: ...
    def seed(self, state: dict[str, Any]) -> dict[str, Any]: ...
    def backfill(
        self, state: dict[str, Any], *, since: datetime, limit: int = 50
    ) -> PollResult: ...
