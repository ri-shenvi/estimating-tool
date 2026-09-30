from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.models import Decision, FieldHistory, Opportunity, Score
from bidtriage.web.deps import CurrentUser, current_user, db

router = APIRouter(tags=["opportunities"])


def _templates():
    from bidtriage.web.app import templates

    return templates


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    session: Session = Depends(db),
    user: CurrentUser = Depends(current_user),
    status: str | None = None,
    band: str | None = None,
):
    stmt = (
        select(Opportunity)
        .where(Opportunity.archived_at.is_(None))
        .order_by(Opportunity.last_activity_at.desc())
        .limit(200)
    )
    if status:
        stmt = stmt.where(Opportunity.status == status)
    opps = session.scalars(stmt).all()
    latest_scores = {
        s.opportunity_id: s
        for s in session.scalars(select(Score).order_by(Score.computed_at.asc())).all()
    }
    rows = []
    for o in opps:
        sc = latest_scores.get(o.id)
        if band and (sc is None or sc.band != band):
            continue
        rows.append({"o": o, "score": sc})
    return _templates().TemplateResponse(
        request, "opportunities.html", {"rows": rows, "user": user, "status": status, "band": band}
    )


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
def detail(
    opp_id: str,
    request: Request,
    session: Session = Depends(db),
    user: CurrentUser = Depends(current_user),
):
    o = session.get(Opportunity, opp_id)
    if o is None:
        raise HTTPException(404)
    score = session.scalars(
        select(Score).where(Score.opportunity_id == opp_id).order_by(Score.computed_at.desc())
    ).first()
    history = session.scalars(
        select(FieldHistory)
        .where(FieldHistory.opportunity_id == opp_id)
        .order_by(FieldHistory.changed_at.desc())
    ).all()
    decisions = session.scalars(
        select(Decision)
        .where(Decision.opportunity_id == opp_id)
        .order_by(Decision.created_at.desc())
    ).all()
    return _templates().TemplateResponse(
        request,
        "opportunity.html",
        {"o": o, "score": score, "history": history, "decisions": decisions, "user": user},
    )
