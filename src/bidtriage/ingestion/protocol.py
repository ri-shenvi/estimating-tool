from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from bidtriage.ingestion.eml import ParsedMessage


@dataclass
class PollResult:
    messages: list[tuple[str, ParsedMessage]]  # (provider_message_id, parsed)
    new_state: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class MailSource(Protocol):
    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult: ...
