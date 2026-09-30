"""Apply an action to an opportunity (SPEC-06 F1). Persistence via SQLAlchemy session; audit written in the same transaction."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from bidtriage.core.clock import Clock, SystemClock
from bidtriage.core.models import AuditEvent, Decision, Opportunity, Outcome
from bidtriage.decisions.state import ACTION_TO_STATUS, transition

PASS_REASONS = {
    "too_big",
    "too_small",
    "wrong_type",
    "gc",
    "too_far",
    "no_capacity",
    "open_shop",
    "other",
}
UNDO_WINDOW = timedelta(days=7)


class ActionResult(dict[str, Any]):
    pass


def apply_action(
    session: Session,
    opp: Opportunity,
    action: str,
    *,
    actor_user_id: str | None,
    actor_role: str | None,
    channel: str,
    reason: str | None = None,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
    clock: Clock | None = None,
) -> ActionResult:
    clock = clock or SystemClock()
    now = clock.now()
    payload = dict(payload or {})
    before = {
        "status": opp.status,
        "assignee": opp.assignee_user_id,
        "snooze_until": opp.snooze_until.isoformat() if opp.snooze_until else None,
    }

    if action == "assign":
        opp.assignee_user_id = payload.get("assignee_user_id")
        if opp.status == "new":
            opp.status = "undecided"
    elif action == "snooze":
        days = int(payload.get("days", 3))
        until = payload.get("until")
        target: datetime = datetime.fromisoformat(until) if until else now + timedelta(days=days)
        bid_due = _bid_due(opp)
        overridden = False
        if bid_due is not None and target > bid_due - timedelta(days=2):
            target = max(now, bid_due - timedelta(days=2))
            overridden = True
        opp.status = transition(opp.status, "snoozed")
        opp.snooze_until = target
        payload["snooze_until"] = target.isoformat()
        payload["snooze_overridden"] = overridden
    elif action == "needs_info":
        opp.flags = list({*opp.flags, "needs_info"})
    elif action == "lock_field":
        opp.locked_fields = list({*opp.locked_fields, payload["field"]})
    elif action in ACTION_TO_STATUS:
        target_status = ACTION_TO_STATUS[action]
        opp.status = transition(opp.status, target_status)
        if action == "bid" and opp.assignee_user_id is None and actor_user_id:
            opp.assignee_user_id = actor_user_id
        if action == "pass" and reason is not None and reason not in PASS_REASONS:
            raise ValueError(f"unknown pass reason {reason}")
        if action == "snooze":
            pass
        if action in ("submit", "won", "lost", "cancel"):
            session.add(
                Outcome(
                    opportunity_id=opp.id,
                    result={
                        "submit": "submitted",
                        "won": "won",
                        "lost": "lost",
                        "cancel": "cancelled",
                    }[action],
                    submitted_at=now if action == "submit" else None,
                    submitted_price=payload.get("price"),
                    award_price=payload.get("award_price"),
                    competitor=payload.get("competitor"),
                    notes=note,
                    created_at=now,
                )
            )
    else:
        raise ValueError(f"unknown action {action}")

    opp.last_activity_at = now
    if action != "snooze":
        opp.snooze_until = None
    decision = Decision(
        opportunity_id=opp.id,
        action=action,
        actor_user_id=actor_user_id,
        channel=channel,
        reason=reason,
        note=note,
        payload=payload,
        created_at=now,
    )
    session.add(decision)
    after = {
        "status": opp.status,
        "assignee": opp.assignee_user_id,
        "snooze_until": opp.snooze_until.isoformat() if opp.snooze_until else None,
    }
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            role=actor_role,
            action=f"opportunity.{action}",
            entity_type="opportunity",
            entity_id=opp.id,
            before=before,
            after=after,
            channel=channel,
            created_at=now,
        )
    )
    session.flush()
    return ActionResult(decision_id=decision.id, before=before, after=after)


def _bid_due(opp: Opportunity) -> datetime | None:
    raw = (opp.canonical.get("bid_due") or {}).get("value")
    if not raw:
        return None
    return datetime.fromisoformat(raw)
