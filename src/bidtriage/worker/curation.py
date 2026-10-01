"""Manual merge, split and undo of opportunities (SPEC-03 F6).

Automatic resolution is deliberately cautious, so a human has to finish the job for the ambiguous
0.6–0.9 band. Everything here is reversible for 30 days, which means every operation records not
just what it did but enough of the previous state to put it back exactly: the removed
opportunity's own row, and the ids of the child rows that moved.

The undo path moves rows back rather than recreating them, so `field_history`, decisions and
outcomes keep their identities and timestamps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import Clock, SystemClock, aware
from bidtriage.core.models import (
    Addendum,
    AuditEvent,
    Decision,
    FieldHistory,
    MergeLog,
    Opportunity,
    OpportunityKey,
    OpportunitySource,
    Outcome,
    RawMessage,
    Score,
)
from bidtriage.resolution.normalize import fingerprint, geohash, normalize_name

log = logging.getLogger("bidtriage.curation")

#: A status that represents a human decision. Two opportunities that each carry one, and disagree,
#: cannot be merged without someone saying which decision survives.
DECIDED_STATUSES = ("bidding", "passed", "submitted", "won", "lost", "cancelled")

#: Opportunity columns a merge or split may change on the surviving row, and therefore the ones
#: undo has to restore.
_RESTORED_COLUMNS = (
    "status",
    "gc_id",
    "canonical",
    "normalized_name",
    "fingerprint",
    "lat",
    "lon",
    "geohash",
    "first_seen_at",
    "last_activity_at",
    "assignee_user_id",
    "snooze_until",
    "changed_since_digest",
    "material_change",
    "change_summary",
    "flags",
    "locked_fields",
    "related_project_ids",
    "archived_at",
)

#: Child tables a merge or split re-parents, with the column that identifies a row inside one.
#: Typed loosely on purpose: the entries have no common base beyond `opportunity_id`.
_MOVED_CHILDREN: tuple[tuple[Any, str], ...] = (
    (OpportunitySource, "message_id"),
    (Addendum, "id"),
    (FieldHistory, "id"),
    (Decision, "id"),
    (Outcome, "id"),
    (Score, "id"),
)


class MergeConflictError(ValueError):
    """Raised when a merge would silently discard a decision someone made."""


class UndoExpiredError(ValueError):
    pass


# ------------------------------------------------------------------ helpers


def _snapshot(opp: Opportunity) -> dict[str, Any]:
    out: dict[str, Any] = {"id": opp.id}
    for name in _RESTORED_COLUMNS:
        value = getattr(opp, name)
        out[name] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _restore(opp: Opportunity, snap: dict[str, Any]) -> None:
    for name in _RESTORED_COLUMNS:
        if name not in snap:
            continue
        value = snap[name]
        if name in ("first_seen_at", "last_activity_at", "snooze_until", "archived_at"):
            value = datetime.fromisoformat(value) if value else None
        setattr(opp, name, value)


def _audit(
    session: Session,
    action: str,
    entity_id: str,
    *,
    actor_user_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    now: datetime,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            entity_type="opportunity",
            entity_id=entity_id,
            before=before,
            after=after,
            channel="review",
            created_at=now,
        )
    )


def _merge_canonical(survivor: Opportunity, victim: Opportunity) -> None:
    """Fill the survivor's blanks from the victim; never overwrite a value it already holds.

    The survivor was chosen by a human, so its own fields win. Scope items and flags are unions,
    exactly as they are when a message merges in (SPEC-03 F3).
    """
    c, v = dict(survivor.canonical), victim.canonical
    for key, value in v.items():
        if key in ("scope_items", "flags", "delivery_channels"):
            continue
        current = c.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            if not current.get("value") and value.get("value"):
                c[key] = value
        elif current in (None, "", "unknown", [], {}) and value not in (None, "", "unknown"):
            c[key] = value
    c["scope_items"] = sorted(set(c.get("scope_items") or []) | set(v.get("scope_items") or []))
    c["delivery_channels"] = sorted(
        set(c.get("delivery_channels") or []) | set(v.get("delivery_channels") or [])
    )
    survivor.canonical = c
    survivor.flags = sorted(set(survivor.flags) | set(victim.flags))
    survivor.locked_fields = sorted(set(survivor.locked_fields) | set(victim.locked_fields))
    survivor.first_seen_at = min(_at(survivor.first_seen_at), _at(victim.first_seen_at))
    survivor.last_activity_at = max(_at(survivor.last_activity_at), _at(victim.last_activity_at))
    survivor.normalized_name = normalize_name(
        (survivor.canonical.get("project_name") or {}).get("value")
    )
    survivor.lat = survivor.lat if survivor.lat is not None else victim.lat
    survivor.lon = survivor.lon if survivor.lon is not None else victim.lon
    survivor.geohash = geohash(survivor.lat, survivor.lon)
    survivor.fingerprint = fingerprint(
        (survivor.canonical.get("project_name") or {}).get("value"),
        survivor.canonical.get("gc_domain"),
        (survivor.canonical.get("location") or {}).get("city"),
        _due(survivor),
    )
    # A merged pair is no longer a possible duplicate of each other.
    survivor.flags = [f for f in survivor.flags if f != "possible_duplicate"]
    survivor.related_project_ids = sorted(
        {*survivor.related_project_ids, *victim.related_project_ids} - {survivor.id, victim.id}
    )


def _due(opp: Opportunity) -> datetime | None:
    return _parse_due(opp.canonical)


def _parse_due(canonical: dict[str, Any]) -> datetime | None:
    raw = (canonical.get("bid_due") or {}).get("value")
    return datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None


def _at(value: datetime | None) -> datetime:
    """A stored timestamp as an aware datetime. These columns are `nullable=False`."""
    result = aware(value)
    if result is None:
        raise ValueError("expected a timestamp")
    return result


# ------------------------------------------------------------------ merge


def merge_opportunities(
    session: Session,
    *,
    survivor_id: str,
    victim_id: str,
    actor_user_id: str | None = None,
    reason: str | None = None,
    clock: Clock | None = None,
) -> MergeLog:
    """Fold `victim` into `survivor`: all sources and history move, the merge is logged.

    Raises `MergeConflictError` when the two carry different decisions — resolving that is a
    judgement call, not something a merge should make silently (SPEC-03 edge cases).
    """
    now = (clock or SystemClock()).now()
    if survivor_id == victim_id:
        raise ValueError("cannot merge an opportunity into itself")
    survivor = session.get(Opportunity, survivor_id)
    victim = session.get(Opportunity, victim_id)
    if survivor is None or victim is None:
        raise LookupError("both opportunities must exist")
    if (
        survivor.status in DECIDED_STATUSES
        and victim.status in DECIDED_STATUSES
        and survivor.status != victim.status
    ):
        raise MergeConflictError(
            f"{survivor_id} is {survivor.status} and {victim_id} is {victim.status}; "
            "set both to the same decision (or reopen one) before merging"
        )

    before: dict[str, Any] = {
        "victim": _snapshot(victim),
        "survivor": _snapshot(survivor),
        "moved": {},
        "dropped": {},
        "related_rewrites": [],
    }

    # Children the survivor already has an identical key for cannot simply move; they are removed
    # and kept verbatim in the log so undo can put them back.
    dropped_sources = []
    for src in session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == victim_id)
    ).all():
        if session.get(OpportunitySource, (survivor_id, src.message_id)) is not None:
            dropped_sources.append(
                {
                    "message_id": src.message_id,
                    "role": src.role,
                    "attached_at": _at(src.attached_at).isoformat(),
                    "evidence": src.evidence,
                }
            )
            session.delete(src)
    before["dropped"]["sources"] = dropped_sources

    dropped_addenda = []
    for add in session.scalars(select(Addendum).where(Addendum.opportunity_id == victim_id)).all():
        twin = session.scalar(
            select(Addendum).where(
                Addendum.opportunity_id == survivor_id, Addendum.label == add.label
            )
        )
        if twin is None:
            continue
        # The same addendum reached us down both paths: one record, the copies added up.
        twin.copies += add.copies
        dropped_addenda.append(
            {
                "id": add.id,
                "label": add.label,
                "number": add.number,
                "message_id": add.message_id,
                "received_at": _at(add.received_at).isoformat(),
                "summary": add.summary,
                "copies": add.copies,
                "absorbed_into": twin.id,
            }
        )
        session.delete(add)
    before["dropped"]["addenda"] = dropped_addenda

    dropped_keys = []
    for key in session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id == victim_id)
    ).all():
        if session.get(OpportunityKey, (survivor_id, key.kind, key.value)) is not None:
            dropped_keys.append({"kind": key.kind, "value": key.value})
            session.delete(key)
    before["dropped"]["keys"] = dropped_keys
    session.flush()

    for model, id_attr in _MOVED_CHILDREN:
        rows: list[Any] = list(
            session.scalars(select(model).where(model.opportunity_id == victim_id)).all()
        )
        before["moved"][model.__tablename__] = [getattr(r, id_attr) for r in rows]
        for row in rows:
            row.opportunity_id = survivor_id
    keys = session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id == victim_id)
    ).all()
    before["moved"]["opportunity_keys"] = [[k.kind, k.value] for k in keys]
    for key in keys:
        key.opportunity_id = survivor_id
    session.flush()

    # Anyone who pointed at the victim as a related project now points at the survivor.
    for other in session.scalars(select(Opportunity).where(Opportunity.id != survivor_id)).all():
        if victim_id in other.related_project_ids:
            before["related_rewrites"].append(
                {"id": other.id, "related_project_ids": list(other.related_project_ids)}
            )
            other.related_project_ids = sorted(
                (set(other.related_project_ids) - {victim_id, other.id}) | {survivor_id}
            )

    _merge_canonical(survivor, victim)
    survivor.changed_since_digest = True
    survivor.change_summary = (
        f"merged with {(victim.canonical.get('project_name') or {}).get('value') or victim_id}"
    )
    session.delete(victim)
    session.flush()

    entry = MergeLog(
        kind="merge",
        survivor_id=survivor_id,
        other_id=victim_id,
        actor_user_id=actor_user_id,
        reason=reason,
        before=before,
        created_at=now,
    )
    session.add(entry)
    _audit(
        session,
        "opportunity.merge",
        survivor_id,
        actor_user_id=actor_user_id,
        before={"survivor": before["survivor"], "victim": before["victim"]},
        after={"survivor": _snapshot(survivor)},
        now=now,
    )
    session.flush()
    return entry


# ------------------------------------------------------------------ split


def split_source(
    session: Session,
    *,
    opportunity_id: str,
    message_id: str,
    actor_user_id: str | None = None,
    reason: str | None = None,
    clock: Clock | None = None,
) -> MergeLog:
    """Move one source message out of an opportunity into a new one of its own (SPEC-03 F6)."""
    now = (clock or SystemClock()).now()
    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        raise LookupError(f"no such opportunity {opportunity_id}")
    src = session.get(OpportunitySource, (opportunity_id, message_id))
    if src is None:
        raise LookupError(f"{message_id} is not a source of {opportunity_id}")
    remaining = session.scalars(
        select(OpportunitySource).where(
            OpportunitySource.opportunity_id == opportunity_id,
            OpportunitySource.message_id != message_id,
        )
    ).all()
    if not remaining:
        raise ValueError("cannot split the only source out of an opportunity")

    msg = session.get(RawMessage, message_id)
    canonical = _canonical_for_split(session, opp, message_id)
    fresh = Opportunity(
        status="new",
        gc_id=opp.gc_id,
        canonical=canonical,
        normalized_name=normalize_name((canonical.get("project_name") or {}).get("value")),
        fingerprint=fingerprint(
            (canonical.get("project_name") or {}).get("value"),
            canonical.get("gc_domain"),
            (canonical.get("location") or {}).get("city"),
            _parse_due(canonical),
        ),
        lat=opp.lat,
        lon=opp.lon,
        geohash=geohash(opp.lat, opp.lon),
        first_seen_at=aware(msg.received_at) if msg else now,
        last_activity_at=now,
        flags=["split_out"],
    )
    session.add(fresh)
    session.flush()

    before: dict[str, Any] = {
        "survivor": _snapshot(opp),
        "new_id": fresh.id,
        "message_id": message_id,
        "moved": {},
    }
    src.opportunity_id = fresh.id
    # Flushed before `_keys_of_remaining_sources` reads back, so "which keys does the parent still
    # earn" does not depend on autoflush being on.
    session.flush()
    addenda = session.scalars(
        select(Addendum).where(
            Addendum.opportunity_id == opportunity_id, Addendum.message_id == message_id
        )
    ).all()
    before["moved"]["addenda"] = [a.id for a in addenda]
    for add in addenda:
        add.opportunity_id = fresh.id
    history = session.scalars(
        select(FieldHistory).where(
            FieldHistory.opportunity_id == opportunity_id, FieldHistory.message_id == message_id
        )
    ).all()
    before["moved"]["field_history"] = [h.id for h in history]
    for h in history:
        h.opportunity_id = fresh.id

    moved_keys: list[list[str]] = []
    if msg is not None:
        from bidtriage.worker.pipeline import message_links, platform_ids, thread_ids

        wanted = {("thread", t) for t in thread_ids(msg)} | {
            ("platform", p) for p in platform_ids(message_links(session, message_id))
        }
        still_needed = _keys_of_remaining_sources(session, opportunity_id)
        for kind, value in sorted(wanted - still_needed):
            key = session.get(OpportunityKey, (opportunity_id, kind, value))
            if key is None:
                continue
            session.delete(key)
            session.flush()
            session.add(OpportunityKey(opportunity_id=fresh.id, kind=kind, value=value))
            moved_keys.append([kind, value])
    before["moved"]["opportunity_keys"] = moved_keys

    opp.related_project_ids = sorted({*opp.related_project_ids, fresh.id})
    fresh.related_project_ids = [opp.id]
    opp.changed_since_digest = True
    opp.change_summary = "a source message was split out into its own opportunity"
    session.flush()

    entry = MergeLog(
        kind="split",
        survivor_id=opportunity_id,
        other_id=fresh.id,
        actor_user_id=actor_user_id,
        reason=reason,
        before=before,
        created_at=now,
    )
    session.add(entry)
    _audit(
        session,
        "opportunity.split",
        opportunity_id,
        actor_user_id=actor_user_id,
        before=before["survivor"],
        after={"new_id": fresh.id, "message_id": message_id},
        now=now,
    )
    session.flush()
    return entry


def _keys_of_remaining_sources(session: Session, opportunity_id: str) -> set[tuple[str, str]]:
    """Hard keys the opportunity still earns from its other source messages."""
    from bidtriage.worker.pipeline import message_links, platform_ids, thread_ids

    keys: set[tuple[str, str]] = set()
    for src in session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == opportunity_id)
    ).all():
        msg = session.get(RawMessage, src.message_id)
        if msg is None:
            continue
        keys |= {("thread", t) for t in thread_ids(msg)}
        keys |= {("platform", p) for p in platform_ids(message_links(session, msg.id))}
    return keys


def _canonical_for_split(session: Session, opp: Opportunity, message_id: str) -> dict[str, Any]:
    """The split-out opportunity's own fields, from that message's extraction if we still have it.

    Falling back to a copy of the parent's canonical record keeps the split usable even for a
    message whose extraction was pruned; the estimator can correct it either way.
    """
    from bidtriage.worker.pipeline import _canonical_from, latest_extraction

    ext = latest_extraction(session, message_id)
    if ext is None:
        return dict(opp.canonical)
    from bidtriage.extraction.schema import ExtractedOpportunity

    x = ExtractedOpportunity.model_validate(ext.payload)
    msg = session.get(RawMessage, message_id)
    return _canonical_from(x, opp.canonical.get("gc_domain"), aware(msg.sent_at) if msg else None)


# ------------------------------------------------------------------ undo


def _reparent_children(session: Session, *, from_id: str, to_id: str) -> None:
    """Move every child row onto another opportunity, absorbing rather than colliding."""
    for src in session.scalars(
        select(OpportunitySource).where(OpportunitySource.opportunity_id == from_id)
    ).all():
        if session.get(OpportunitySource, (to_id, src.message_id)) is not None:
            session.delete(src)
    for add in session.scalars(select(Addendum).where(Addendum.opportunity_id == from_id)).all():
        twin = session.scalar(
            select(Addendum).where(Addendum.opportunity_id == to_id, Addendum.label == add.label)
        )
        if twin is not None:
            twin.copies += add.copies
            session.delete(add)
    for key in session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id == from_id)
    ).all():
        if session.get(OpportunityKey, (to_id, key.kind, key.value)) is not None:
            session.delete(key)
    session.flush()

    for model, _ in _MOVED_CHILDREN:
        rows: list[Any] = list(
            session.scalars(select(model).where(model.opportunity_id == from_id)).all()
        )
        for row in rows:
            row.opportunity_id = to_id
    for key in session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id == from_id)
    ).all():
        key.opportunity_id = to_id
    session.flush()


def undo(
    session: Session,
    *,
    merge_log_id: str,
    actor_user_id: str | None = None,
    clock: Clock | None = None,
    window_days: int | None = None,
) -> MergeLog:
    """Reverse a merge or a split, within the undo window (SPEC-03 F6)."""
    now = (clock or SystemClock()).now()
    entry = session.get(MergeLog, merge_log_id)
    if entry is None:
        raise LookupError(f"no such merge log entry {merge_log_id}")
    if entry.undone_at is not None:
        raise ValueError("already undone")
    if window_days is None:
        from bidtriage.core.config import get_settings

        window_days = get_settings().merge_undo_days
    created = aware(entry.created_at)
    if created is not None and now - created > timedelta(days=window_days):
        raise UndoExpiredError(f"the {window_days}-day undo window for {merge_log_id} has passed")
    (_undo_merge if entry.kind == "merge" else _undo_split)(session, entry, now)
    entry.undone_at = now
    entry.undone_by_user_id = actor_user_id
    _audit(
        session,
        f"opportunity.{entry.kind}.undo",
        entry.survivor_id,
        actor_user_id=actor_user_id,
        before=None,
        after={"restored": entry.other_id},
        now=now,
    )
    session.flush()
    return entry


def _undo_merge(session: Session, entry: MergeLog, now: datetime) -> None:
    before = entry.before
    survivor = session.get(Opportunity, entry.survivor_id)
    if survivor is None:
        raise LookupError(f"survivor {entry.survivor_id} is gone; cannot undo")
    snap = before["victim"]
    victim = Opportunity(id=snap["id"])
    _restore(victim, snap)
    session.add(victim)
    session.flush()

    for model, id_attr in _MOVED_CHILDREN:
        ids = before["moved"].get(model.__tablename__, [])
        if not ids:
            continue
        rows: list[Any] = list(
            session.scalars(
                select(model).where(
                    model.opportunity_id == entry.survivor_id,
                    getattr(model, id_attr).in_(ids),
                )
            ).all()
        )
        for row in rows:
            row.opportunity_id = victim.id
    for kind, value in before["moved"].get("opportunity_keys", []):
        key = session.get(OpportunityKey, (entry.survivor_id, kind, value))
        if key is not None:
            session.delete(key)
            session.flush()
            session.add(OpportunityKey(opportunity_id=victim.id, kind=kind, value=value))

    for row in before["dropped"].get("sources", []):
        session.add(
            OpportunitySource(
                opportunity_id=victim.id,
                message_id=row["message_id"],
                role=row["role"],
                attached_at=datetime.fromisoformat(row["attached_at"]),
                evidence=row["evidence"],
            )
        )
    for row in before["dropped"].get("addenda", []):
        twin = session.get(Addendum, row["absorbed_into"])
        if twin is not None:
            twin.copies = max(1, twin.copies - row["copies"])
        session.add(
            Addendum(
                id=row["id"],
                opportunity_id=victim.id,
                label=row["label"],
                number=row["number"],
                message_id=row["message_id"],
                received_at=datetime.fromisoformat(row["received_at"]),
                summary=row["summary"],
                copies=row["copies"],
            )
        )
    for row in before["dropped"].get("keys", []):
        session.add(OpportunityKey(opportunity_id=victim.id, kind=row["kind"], value=row["value"]))

    for rewrite in before.get("related_rewrites", []):
        other = session.get(Opportunity, rewrite["id"])
        if other is not None:
            other.related_project_ids = rewrite["related_project_ids"]
    _restore(survivor, before["survivor"])
    survivor.changed_since_digest = True
    survivor.change_summary = "merge undone"
    session.flush()


def _undo_split(session: Session, entry: MergeLog, now: datetime) -> None:
    before = entry.before
    survivor = session.get(Opportunity, entry.survivor_id)
    fresh = session.get(Opportunity, entry.other_id)
    if survivor is None or fresh is None:
        raise LookupError("both sides of the split must still exist to undo it")
    # Everything on the split-out opportunity goes home, not just the rows the split moved: work
    # attached to it afterwards would otherwise be orphaned by the delete below. Rows the survivor
    # already has an identical key for cannot simply move — `addenda` is unique on
    # (opportunity, label) and `opportunity_sources` is keyed on (opportunity, message) — so those
    # are absorbed the way a merge absorbs them.
    _reparent_children(session, from_id=fresh.id, to_id=survivor.id)
    session.delete(fresh)
    _restore(survivor, before["survivor"])
    survivor.changed_since_digest = True
    survivor.change_summary = "split undone"
    session.flush()


def undoable(session: Session, *, now: datetime, window_days: int = 30) -> list[MergeLog]:
    """Merges and splits still inside their undo window, newest first."""
    cutoff = now - timedelta(days=window_days)
    return list(
        session.scalars(
            select(MergeLog)
            .where(MergeLog.undone_at.is_(None), MergeLog.created_at >= cutoff)
            .order_by(MergeLog.created_at.desc())
        ).all()
    )
