"""JSON API kept clean for future clients (ADR-006)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import SystemClock, aware
from bidtriage.core.config import Settings, get_settings
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import GC, Opportunity, OpportunitySource, RawMessage, Score
from bidtriage.decisions.actions import apply_action
from bidtriage.decisions.state import InvalidTransitionError
from bidtriage.web.deps import CurrentUser, current_user, db, require_role
from bidtriage.worker import curation, pipeline

router = APIRouter(prefix="/api", tags=["api"])


class ActionBody(BaseModel):
    action: str
    reason: str | None = None
    note: str | None = None
    payload: dict[str, Any] = {}


class MergeBody(BaseModel):
    """`victim_id` is folded into the opportunity in the path, which survives."""

    victim_id: str
    reason: str | None = None


class SplitBody(BaseModel):
    message_id: str
    reason: str | None = None


@router.get("/opportunities")
def list_opportunities(
    session: Session = Depends(db),
    status: str | None = None,
    band: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = (
        select(Opportunity)
        .where(Opportunity.archived_at.is_(None))
        .order_by(Opportunity.last_activity_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Opportunity.status == status)
    out = []
    for o in session.scalars(stmt).all():
        sc = session.scalars(
            select(Score).where(Score.opportunity_id == o.id).order_by(Score.computed_at.desc())
        ).first()
        if band and (sc is None or sc.band != band):
            continue
        out.append(
            {
                "id": o.id,
                "status": o.status,
                "score": sc.score if sc else None,
                "band": sc.band if sc else None,
                "canonical": o.canonical,
                "assignee": o.assignee_user_id,
            }
        )
    return out


@router.get("/opportunities/{opp_id}")
def get_opportunity(opp_id: str, session: Session = Depends(db)) -> dict[str, Any]:
    o = session.get(Opportunity, opp_id)
    if o is None:
        raise HTTPException(404)
    sc = session.scalars(
        select(Score).where(Score.opportunity_id == o.id).order_by(Score.computed_at.desc())
    ).first()
    return {
        "id": o.id,
        "status": o.status,
        "canonical": o.canonical,
        "flags": o.flags,
        "score": sc.explanation if sc else None,
    }


@router.post("/opportunities/{opp_id}/actions")
def post_action(
    opp_id: str,
    body: ActionBody,
    session: Session = Depends(db),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    o = session.get(Opportunity, opp_id)
    if o is None:
        raise HTTPException(404)
    try:
        res = apply_action(
            session,
            o,
            body.action,
            actor_user_id=None if user.id == "dev" else user.id,
            actor_role=user.role,
            channel="api",
            reason=body.reason,
            note=body.note,
            payload=body.payload,
            clock=SystemClock(),
        )
    except (InvalidTransitionError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return dict(res)


@router.get("/gcs")
def list_gcs(session: Session = Depends(db)) -> list[dict[str, Any]]:
    return [
        {
            "id": g.id,
            "name": g.canonical_name,
            "tier": g.tier,
            "key_account": g.key_account,
            "domains": [d.domain for d in g.domains],
        }
        for g in session.scalars(select(GC).order_by(GC.canonical_name)).all()
    ]


# ------------------------------------------------------------ merge, split, undo (SPEC-03 F6)


def _rescore(session: Session, opportunity_id: str) -> None:
    enqueue(
        session,
        "score_opportunity",
        f"score:{opportunity_id}:curated:{SystemClock().now().isoformat(timespec='seconds')}",
        {"opportunity_id": opportunity_id},
    )


@router.post("/opportunities/{opp_id}/merge")
def post_merge(
    opp_id: str,
    body: MergeBody,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("estimator", "chief", "admin")),
) -> dict[str, Any]:
    """Fold another opportunity into this one. Reversible for `MERGE_UNDO_DAYS`."""
    try:
        entry = curation.merge_opportunities(
            session,
            survivor_id=opp_id,
            victim_id=body.victim_id,
            actor_user_id=None if user.id == "dev" else user.id,
            reason=body.reason,
            clock=SystemClock(),
        )
    except curation.MergeConflictError as e:
        raise HTTPException(409, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _rescore(session, opp_id)
    return {"merge_log_id": entry.id, "survivor_id": opp_id, "merged": body.victim_id}


@router.post("/opportunities/{opp_id}/split")
def post_split(
    opp_id: str,
    body: SplitBody,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("estimator", "chief", "admin")),
) -> dict[str, Any]:
    """Move one source message out of this opportunity into a new one."""
    try:
        entry = curation.split_source(
            session,
            opportunity_id=opp_id,
            message_id=body.message_id,
            actor_user_id=None if user.id == "dev" else user.id,
            reason=body.reason,
            clock=SystemClock(),
        )
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _rescore(session, opp_id)
    _rescore(session, entry.other_id)
    return {
        "merge_log_id": entry.id,
        "opportunity_id": opp_id,
        "new_opportunity_id": entry.other_id,
    }


@router.post("/merges/{merge_log_id}/undo")
def post_undo(
    merge_log_id: str,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("estimator", "chief", "admin")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        entry = curation.undo(
            session,
            merge_log_id=merge_log_id,
            actor_user_id=None if user.id == "dev" else user.id,
            clock=SystemClock(),
            window_days=settings.merge_undo_days,
        )
    except curation.UndoExpiredError as e:
        raise HTTPException(410, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _rescore(session, entry.survivor_id)
    return {"undone": entry.kind, "survivor_id": entry.survivor_id, "restored": entry.other_id}


@router.get("/opportunities/{opp_id}/duplicates")
def get_duplicates(
    opp_id: str, session: Session = Depends(db), limit: int = 10
) -> list[dict[str, Any]]:
    """The opportunities this one most resembles, for the review page's merge picker (SPEC-03 F2.3)."""
    o = session.get(Opportunity, opp_id)
    if o is None:
        raise HTTPException(404)
    return [
        {
            "id": other.id,
            "project_name": (other.canonical.get("project_name") or {}).get("value"),
            "gc_name": (other.canonical.get("gc_name") or {}).get("value"),
            "status": other.status,
            "score": ev.score,
            "hard_key": ev.hard_key,
            "notes": ev.notes,
        }
        for other, ev in pipeline.possible_duplicates(session, o, limit=limit)
    ]


@router.get("/merges")
def list_merges(
    session: Session = Depends(db), settings: Settings = Depends(get_settings)
) -> list[dict[str, Any]]:
    """Merges and splits still inside the undo window."""
    return [
        {
            "id": m.id,
            "kind": m.kind,
            "survivor_id": m.survivor_id,
            "other_id": m.other_id,
            "created_at": aware(m.created_at).isoformat(),  # type: ignore[union-attr]
            "actor_user_id": m.actor_user_id,
            "reason": m.reason,
        }
        for m in curation.undoable(
            session, now=SystemClock().now(), window_days=settings.merge_undo_days
        )
    ]


@router.get("/opportunities/{opp_id}/sources")
def get_sources(opp_id: str, session: Session = Depends(db)) -> list[dict[str, Any]]:
    """The messages behind an opportunity, with the role each played (SPEC-03 F1)."""
    rows = session.scalars(
        select(OpportunitySource)
        .where(OpportunitySource.opportunity_id == opp_id)
        .order_by(OpportunitySource.attached_at)
    ).all()
    out = []
    for src in rows:
        msg = session.get(RawMessage, src.message_id)
        out.append(
            {
                "message_id": src.message_id,
                "role": src.role,
                "attached_at": aware(src.attached_at).isoformat(),  # type: ignore[union-attr]
                "subject": msg.subject if msg else None,
                "from": msg.from_addr if msg else None,
                "evidence": src.evidence,
            }
        )
    return out
