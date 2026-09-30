"""Field merge rules (SPEC-03 F3): dates follow latest-message-wins except reminders may only confirm.

Everything here is a pure function over plain values. The database work that uses it lives in
`worker/pipeline.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any

#: Kinds whose stated dates are allowed to move a date we already hold. A `reminder` is absent on
#: purpose: it may only confirm (SPEC-03 F3).
DATE_MOVERS = {"itb", "rfb", "addendum", "date_change", "prebid_notice"}

#: How a tracked date reads in a change summary, which an estimator sees in the digest. The field
#: names are for the history table; "Due date moved Oct 16 -> Oct 21" is for a person (SPEC-03 F4).
FIELD_LABELS = {
    "bid_due": "Due date",
    "rfi_deadline": "RFI deadline",
    "intent_due": "Intent to bid",
    "prebid": "Pre-bid",
}


def label_for(field: str) -> str:
    return FIELD_LABELS.get(field, field.replace("_", " "))


#: Fields whose every change is recorded in `field_history` (SPEC-03 F4).
TRACKED_FIELDS = (
    "bid_due",
    "prebid",
    "rfi_deadline",
    "scope_items",
    "flags",
    "bid_type",
    "size_signals",
    "addenda",
    "status",
)


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
    name = label_for(field)
    if incoming is None:
        return DateMergeOutcome(existing, False, False, "no incoming value")
    if locked:
        if existing is not None and existing != incoming:
            return DateMergeOutcome(
                existing, False, True, f"{name} locked by user; system saw {incoming:%b %d}"
            )
        return DateMergeOutcome(existing, False, False, "locked; no change")
    if existing is None:
        return DateMergeOutcome(incoming, True, False, f"{name} set to {incoming:%b %d}")
    if incoming_kind == "reminder":
        if existing.date() == incoming.date():
            return DateMergeOutcome(existing, False, False, "reminder confirms date")
        return DateMergeOutcome(
            existing,
            False,
            True,
            f"reminder says {incoming:%b %d} but {name.lower()} is {existing:%b %d}",
        )
    if incoming_kind in DATE_MOVERS:
        if existing == incoming:
            return DateMergeOutcome(existing, False, False, "unchanged")
        return DateMergeOutcome(
            incoming, True, False, f"{name} moved {existing:%b %d} → {incoming:%b %d}"
        )
    return DateMergeOutcome(existing, False, False, f"{incoming_kind} does not move dates")


# ------------------------------------------------------------------ addenda


def addendum_gaps(numbers: list[int]) -> list[int]:
    present = sorted({n for n in numbers if n is not None})
    if not present:
        return []
    return [n for n in range(1, present[-1]) if n not in present]


def gap_detection_enabled(labels: list[str | None]) -> bool:
    """True only for a single, consistently numbered sequence.

    "Addendum A" has no number at all, and "Bulletin 1" alongside "Addendum 3" is two sequences
    that happen to share an opportunity — neither can tell us whether something is missing. A gap
    we cannot be sure about is worse than no gap report, because the digest states it as fact, so
    the check is switched off for that opportunity (SPEC-03 edge cases).
    """
    present = [lbl for lbl in labels if lbl]
    if not present:
        return True
    if any(_label_number(lbl) is None for lbl in present):
        return False
    return len({_label_scheme(lbl) for lbl in present}) == 1


def _label_number(label: str | None) -> int | None:
    if not label:
        return None
    m = re.search(r"\b(\d{1,3})\b", label)
    return int(m.group(1)) if m else None


def _label_scheme(label: str) -> str:
    """The wording around the number: "Addendum 2" and "Addendum No. 3" are one scheme."""
    without_number = re.sub(r"\b\d{1,3}\b", " ", label.lower())
    words = [w for w in re.split(r"[^a-z]+", without_number) if w and w not in ("no", "num")]
    return " ".join(words)


# ------------------------------------------------------------------ scope

#: Words that turn an addendum's prose into an instruction to take something out of scope. Removal
#: needs to be stated: silence in a later message never removes scope (SPEC-03 F3).
_REMOVAL_RE = re.compile(
    r"\b(remove[sd]?|removal|delete[sd]?|deletion|descope[sd]?|de-scope[sd]?|"
    r"omit(?:s|ted)?|no longer (?:included|in scope|part of)|strike[sn]?|excluded from this bid)\b",
    re.I,
)
_SENTENCE_RE = re.compile(r"[.;\n]")


@dataclass
class ScopeMergeOutcome:
    value: list[str]
    added: list[str] = dc_field(default_factory=list)
    removed: list[str] = dc_field(default_factory=list)


def states_removal(text: str | None) -> bool:
    return bool(text) and bool(_REMOVAL_RE.search(text or ""))


def removed_scope_items(text: str | None, existing: list[str]) -> list[str]:
    """Scope items an addendum explicitly takes out.

    Conservative on purpose: an item is removed only when it is named in the same clause as a
    removal verb. Dropping scope we should have bid is worse than carrying scope we should not.
    """
    if not text:
        return []
    out: set[str] = set()
    for clause in _SENTENCE_RE.split(text):
        if not _REMOVAL_RE.search(clause):
            continue
        words = f" {re.sub(r'[^a-z0-9]+', ' ', clause.lower()).strip()} "
        matched = {item for item in existing if f" {item.replace('_', ' ')} " in words}
        # "site lighting is removed" names site_lighting, not lighting. Keep only the most
        # specific reading, or a removal would take neighbouring scope with it.
        out |= {
            item
            for item in matched
            if not any(
                other != item and item.replace("_", " ") in other.replace("_", " ")
                for other in matched
            )
        }
    return sorted(out)


def merge_scope(
    existing: list[str], incoming: list[str], *, changes_described: str | None = None
) -> ScopeMergeOutcome:
    """Union, minus whatever an addendum says in words that it removes (SPEC-03 F3)."""
    merged = set(existing) | set(incoming)
    removed = set(removed_scope_items(changes_described, sorted(merged)))
    merged -= removed
    return ScopeMergeOutcome(
        value=sorted(merged),
        added=sorted(set(incoming) - set(existing) - removed),
        removed=sorted(removed & set(existing)),
    )


# ------------------------------------------------------------------ size and materiality

_SIZE_KEYS = (
    "stated_electrical_value",
    "stated_project_value",
    "square_feet",
    "stories",
    "units_or_beds",
)


def size_change_ratio(old: dict[str, Any] | None, new: dict[str, Any] | None) -> float:
    """Largest relative move across the size signals both records state.

    0.0 when nothing comparable changed; a value appearing where there was none is not a change in
    size, it is a change in what we know, so it does not count here.
    """
    if not old or not new:
        return 0.0
    worst = 0.0
    for key in _SIZE_KEYS:
        a, b = old.get(key), new.get(key)
        if not a or not b:
            continue
        worst = max(worst, abs(float(b) - float(a)) / float(a))
    return worst


def size_signals_differ(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    """True when any tracked size signal takes a different value, including first values."""
    old, new = old or {}, new or {}
    return any(new.get(k) is not None and new.get(k) != old.get(k) for k in _SIZE_KEYS)


#: SPEC-03 F5: the two changes big enough to be worth telling someone who already passed.
MATERIAL_DUE_DAYS = 7
MATERIAL_SIZE_RATIO = 0.5


def material_change(
    *,
    old_due: datetime | None = None,
    new_due: datetime | None = None,
    old_size: dict[str, Any] | None = None,
    new_size: dict[str, Any] | None = None,
) -> bool:
    # `timedelta.days` floors, so it reports 7 for a 7d20h move forward and 8 for the same move
    # backward. Seconds keep the threshold symmetric.
    if old_due and new_due:
        moved_days = abs((new_due - old_due).total_seconds()) / 86400
        if moved_days > MATERIAL_DUE_DAYS:
            return True
    return size_change_ratio(old_size, new_size) > MATERIAL_SIZE_RATIO


# ------------------------------------------------------------------ award language

_CANCEL_RE = re.compile(
    r"\b(cancel(?:led|ed|lation|ation)?|withdraw(?:n|al)?|(?:project|bid|solicitation)"
    r"\s+(?:is\s+)?(?:on\s+hold|shelved|postponed indefinitely)|not (?:going|moving) forward)\b",
    re.I,
)
_LOST_RE = re.compile(
    r"\b(awarded to (?:another|a different|other)|another (?:firm|contractor|bidder)|"
    r"unsuccessful|were not (?:the )?(?:successful|low)|not been selected|"
    r"regret to inform|thank you for bidding|decided to (?:go|proceed) with another)\b",
    re.I,
)
_WON_RE = re.compile(
    r"\b(pleased to (?:award|inform)|you (?:have been|are being) awarded|"
    r"(?:intent|letter) of intent to award(?: to you)?|congratulations[, ].{0,40}\baward)\b",
    re.I,
)


def award_outcome(*texts: str | None) -> str | None:
    """Read an `award` message as `cancelled`, `lost` or `won` — or None if it is unclear.

    Cancellation wins over the rest: "cancelled, so nobody was awarded" must not read as a loss.
    Ambiguity returns None, which leaves the status alone for an estimator to set (SPEC-03 F5).
    """
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return None
    if _CANCEL_RE.search(blob):
        return "cancelled"
    won, lost = bool(_WON_RE.search(blob)), bool(_LOST_RE.search(blob))
    if won and not lost:
        return "won"
    if lost and not won:
        return "lost"
    return None


# ------------------------------------------------------------------ summaries


def with_change_count(note: str, count: int) -> str:
    """ "Due date moved Oct 16 → Oct 21" plus "(4 changes)" once there is a history worth counting.

    The digest shows the latest move and how many there have been, not every one of them
    (SPEC-03 edge cases).
    """
    return f"{note} ({count} changes)" if count > 1 else note
