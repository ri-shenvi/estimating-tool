"""Field merge rules (SPEC-03 F3): dates follow latest-message-wins except reminders may only confirm."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DATE_MOVERS = {"itb", "addendum", "date_change"}


@dataclass
class DateMergeOutcome:
    value: datetime | None
    changed: bool
    conflict: bool
    note: str


def merge_date(
    existing: datetime | None,
    incoming: datetime | None,
    *,
    incoming_kind: str,
    locked: bool = False,
    field: str = "bid_due",
) -> DateMergeOutcome:
    if incoming is None:
        return DateMergeOutcome(existing, False, False, "no incoming value")
    if locked:
        if existing is not None and existing != incoming:
            return DateMergeOutcome(
                existing, False, True, f"{field} locked by user; system saw {incoming.isoformat()}"
            )
        return DateMergeOutcome(existing, False, False, "locked; no change")
    if existing is None:
        return DateMergeOutcome(incoming, True, False, f"{field} set")
    if incoming_kind == "reminder":
        if existing.date() == incoming.date():
            return DateMergeOutcome(existing, False, False, "reminder confirms date")
        return DateMergeOutcome(
            existing,
            False,
            True,
            f"reminder says {incoming.isoformat()} but {field} is {existing.isoformat()}",
        )
    if incoming_kind in DATE_MOVERS or incoming_kind == "prebid_notice":
        if existing == incoming:
            return DateMergeOutcome(existing, False, False, "unchanged")
        return DateMergeOutcome(
            incoming, True, False, f"{field} moved {existing:%b %d} → {incoming:%b %d}"
        )
    return DateMergeOutcome(existing, False, False, f"{incoming_kind} does not move dates")


def addendum_gaps(numbers: list[int]) -> list[int]:
    present = sorted(set(n for n in numbers if n is not None))
    if not present:
        return []
    return [n for n in range(1, present[-1]) if n not in present]
