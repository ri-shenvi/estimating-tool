"""Opportunity status lifecycle (SPEC-03 F5)."""

from __future__ import annotations

#: SPEC-03 F5. Two additions the F5 diagram does not draw but the rest of the spec requires:
#:
#: * `lost` from the pre-decision states. The edge-case table says an "awarded to another firm"
#:   letter sets `lost` without conditioning on what we had decided, and a job we were invited to
#:   but never answered is just as over as one we bid.
#: * `cancelled` from every status, because F5 spells that one out as "any status" — an owner
#:   killing a project after it was awarded is exactly the case worth carrying.
#:
#: `won` is deliberately *not* reachable without `submitted`: we cannot win something we never bid,
#: so an award email claiming otherwise is a contradiction. Resolution surfaces it as
#: `outcome_conflict` with an unapplied `field_history` row rather than applying or dropping it.
#:
#: An outcome read out of an award email can still be wrong, so the terminal states stay mutually
#: reachable and an estimator can correct one.
ALLOWED: dict[str, set[str]] = {
    "new": {"undecided", "bidding", "passed", "snoozed", "cancelled", "archived", "lost"},
    "undecided": {"bidding", "passed", "snoozed", "cancelled", "archived", "lost"},
    "snoozed": {"undecided", "bidding", "passed", "cancelled", "archived", "lost"},
    "bidding": {"submitted", "passed", "cancelled", "archived", "undecided", "lost"},
    "passed": {"undecided", "bidding", "cancelled", "archived", "lost"},
    "submitted": {"won", "lost", "cancelled", "archived", "bidding"},
    "won": {"archived", "lost", "submitted", "cancelled"},
    "lost": {"archived", "won", "submitted", "cancelled"},
    "cancelled": {"archived", "undecided"},
    "archived": {"undecided", "bidding", "cancelled"},  # reactivation
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
