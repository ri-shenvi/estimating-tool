"""The contract between a mail transport and the poller (SPEC-01 F7, SPEC-10 F1/F2).

A source hands back a `FetchSession` rather than a list of messages. Two properties matter:

* **Streaming.** The session is an iterator, so exactly one message is resident at a time and a
  batch of drawing sets cannot exhaust the worker (SPEC-10 F2).
* **Cursor safety.** The session does not decide its own new state. The poller reports, per message,
  whether it was durably stored, and only then does `new_state()` compute a cursor. A message that
  failed to fetch or store therefore stays in front of the cursor and is retried (SPEC-10 F1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bidtriage.ingestion.eml import ParsedMessage


@dataclass
class FetchedMessage:
    provider_message_id: str
    parsed: ParsedMessage
    received_at: datetime | None = None
    """Transport-authoritative receipt time (Graph `receivedDateTime`, IMAP `INTERNALDATE`).

    Preferred over the message's own `Received:` header, which the sender controls (SPEC-10 F7).
    """

    @property
    def attachment_bytes(self) -> int:
        return sum(a.size for a in self.parsed.attachments)


@dataclass
class FetchOptions:
    limit: int = 200
    max_bytes: int = 256 * 1024 * 1024
    since: datetime | None = None
    """Set for a backfill walk; None for a live poll."""
    skip_ids: frozenset[str] = frozenset()
    """Provider ids ingestion has given up on; the cursor may advance past them (SPEC-10 F1)."""


class FetchSession(ABC):
    """Bookkeeping shared by every transport's fetch session.

    Subclasses implement `_messages` (the lazy fetch) and `new_state` (the cursor rule).
    """

    can_stop_midstream = True
    """Whether the batch may end between any two messages.

    True when the transport can resume from an arbitrary message (IMAP resumes from a UID). False
    when its cursor covers a whole group — a Graph delta page is only resumable once the page is
    finished — in which case the subclass stops at its own boundary by consulting `budget_spent`.
    """

    def __init__(self, state: dict[str, Any], options: FetchOptions) -> None:
        self.state = state
        self.options = options
        self.errors: list[str] = []
        """Folder-level problems, not attributable to one message."""
        self.failures: dict[str, str] = {}
        """provider id -> error, for messages that could not be fetched or stored."""
        self.stored: set[str] = set()
        self.passed_over: set[str] = set()
        """Ids this batch declined to fetch because ingestion has given up on them (SPEC-10 F1)."""
        self.yielded: list[str] = []
        self.more_available = False
        self._bytes = 0

    # ------------------------------------------------------------------ iteration

    def __iter__(self) -> Iterator[FetchedMessage]:
        for item in self._messages():
            size = item.attachment_bytes
            # The first message is always yielded even if it alone exceeds the budget, so one
            # oversized bid package can never wedge the mailbox (SPEC-10 F2).
            over_budget = bool(self._bytes) and self._bytes + size > self.options.max_bytes
            if over_budget or len(self.yielded) >= self.options.limit:
                self.more_available = True
                if self.can_stop_midstream:
                    return
            self._bytes += size
            self.yielded.append(item.provider_message_id)
            yield item

    @property
    def budget_spent(self) -> bool:
        """True once this batch has done as much as it should; checked at transport boundaries."""
        return self._bytes >= self.options.max_bytes or len(self.yielded) >= self.options.limit

    @abstractmethod
    def _messages(self) -> Iterator[FetchedMessage]:
        """Fetch messages lazily, recording per-message problems via `fail`."""

    # ------------------------------------------------------------------ outcomes

    def record(self, provider_message_id: str, *, stored: bool, error: str | None = None) -> None:
        if stored:
            self.stored.add(provider_message_id)
            self.failures.pop(provider_message_id, None)
        else:
            self.failures[provider_message_id] = error or "not stored"

    def fail(self, provider_message_id: str | None, error: str) -> None:
        if provider_message_id is None:
            self.errors.append(error)
        else:
            self.failures[provider_message_id] = error

    def pass_over(self, provider_message_id: str) -> bool:
        """True when this id has been given up on, recording that the batch stepped over it."""
        if provider_message_id in self.options.skip_ids:
            self.passed_over.add(provider_message_id)
            return True
        return False

    def settled(self, provider_message_id: str) -> bool:
        """True when the cursor may pass this message: it was stored, or it was given up on."""
        return provider_message_id in self.stored or provider_message_id in self.options.skip_ids

    @abstractmethod
    def new_state(self) -> dict[str, Any]:
        """The cursor to persist, given what was actually stored. Never skips an unsettled message."""

    @property
    def all_errors(self) -> list[str]:
        return [*self.errors, *(f"{pid}: {err}" for pid, err in sorted(self.failures.items()))]


class CheckpointSession(FetchSession):
    """For transports whose cursor is a sequence of checkpoints (a page link or an ordered UID).

    Checkpoints are grouped, normally by folder, because each folder carries an independent cursor:
    a folder stuck behind an unreadable message must not freeze the others. Within a group,
    `safe_cursors` keeps the newest checkpoint whose messages — and every message before it in that
    group — were settled.
    """

    def __init__(self, state: dict[str, Any], options: FetchOptions) -> None:
        super().__init__(state, options)
        self._checkpoints: dict[str, list[tuple[Any, list[str]]]] = {}
        self._pending: dict[str, list[str]] = {}

    def note(self, group: str, provider_message_id: str) -> None:
        self._pending.setdefault(group, []).append(provider_message_id)

    def checkpoint(self, group: str, cursor: Any) -> None:
        pending = self._pending.pop(group, [])
        self._checkpoints.setdefault(group, []).append((cursor, pending))

    def safe_cursors(self) -> dict[str, Any]:
        """Per group, the newest checkpoint reachable without stepping over an unsettled message.

        A group whose very first checkpoint is unsafe is absent from the result, meaning "leave the
        stored cursor alone and try again".
        """
        out: dict[str, Any] = {}
        for group, checkpoints in self._checkpoints.items():
            for cursor, ids in checkpoints:
                if not all(self.settled(pid) for pid in ids):
                    break
                out[group] = cursor
        return out


@runtime_checkable
class MailSource(Protocol):
    can_backfill: bool
    """Whether `fetch` honours `FetchOptions.since` to walk history oldest-first."""

    def fetch(self, state: dict[str, Any], options: FetchOptions) -> FetchSession: ...


@runtime_checkable
class BackfillableSource(MailSource, Protocol):
    """A source that can walk history *and* park its live cursor at now (SPEC-01 F7).

    The two come as a pair: seeding a source that cannot walk history would skip that history
    entirely, so `supports_backfill` requires both (SPEC-10 F5).
    """

    def seed(self, state: dict[str, Any]) -> dict[str, Any]: ...


def supports_backfill(impl: object) -> bool:
    """True when a source can both skip live history and walk it (SPEC-10 F5)."""
    return bool(getattr(impl, "can_backfill", False)) and hasattr(impl, "seed")


@dataclass
class PollSummary:
    """What one poll did. Mirrors the `source_polls` row."""

    seen: int = 0
    new: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    more_available: bool = False
