"""Opportunity status lifecycle (SPEC-03 F5)."""

from __future__ import annotations

ALLOWED: dict[str, set[str]] = {
    "new": {"undecided", "bidding", "passed", "snoozed", "cancelled", "archived"},
    "undecided": {"bidding", "passed", "snoozed", "cancelled", "archived"},
    "snoozed": {"undecided", "bidding", "passed", "cancelled", "archived"},
    "bidding": {"submitted", "passed", "cancelled", "archived", "undecided"},
    "passed": {"undecided", "bidding", "cancelled", "archived"},
    "submitted": {"won", "lost", "cancelled", "archived", "bidding"},
    "won": {"archived"},
    "lost": {"archived"},
    "cancelled": {"archived", "undecided"},
    "archived": {"undecided", "bidding"},  # reactivation
}

ACTION_TO_STATUS = {
    "bid": "bidding",
    "pass": "passed",
    "snooze": "snoozed",
    "submit": "submitted",
    "won": "won",
    "lost": "lost",
    "cancel": "cancelled",
    "archive": "archived",
    "reopen": "undecided",
}


class InvalidTransitionError(ValueError):
    pass


def transition(current: str, target: str) -> str:
    if current == target:
        return current
    allowed = ALLOWED.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"cannot move {current} → {target}; allowed: {sorted(allowed)}"
        )
    return target
