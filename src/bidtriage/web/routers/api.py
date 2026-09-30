"""JSON API kept clean for future clients (ADR-006)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import SystemClock
from bidtriage.core.models import GC, Opportunity, Score
from bidtriage.decisions.actions import apply_action
from bidtriage.decisions.state import InvalidTransitionError
from bidtriage.web.deps import CurrentUser, current_user, db

router = APIRouter(prefix="/api", tags=["api"])


class ActionBody(BaseModel):
    action: str
    reason: str | None = None
    note: str | None = None
    payload: dict[str, Any] = {}


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
