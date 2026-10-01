from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import SystemClock
from bidtriage.core.config import Settings, get_settings
from bidtriage.core.models import (
    Addendum,
    Decision,
    FieldHistory,
    Opportunity,
    OpportunitySource,
    RawMessage,
    Score,
)
from bidtriage.web.deps import CurrentUser, current_user, db
from bidtriage.worker import curation, pipeline

router = APIRouter(tags=["opportunities"])


def _templates():
    from bidtriage.web.app import templates

    return templates


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    session: Session = Depends(db),
    user: CurrentUser = Depends(current_user),
    settings: Settings = Depends(get_settings),
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
    rows: list[dict[str, Any]] = []
    for o in opps:
        sc = latest_scores.get(o.id)
        if band and (sc is None or sc.band != band):
            continue
        rows.append({"o": o, "score": sc})
    # SPEC-03 F2.3 / F6: the 0.6-0.9 band and the orphan stubs are the review queue, and every
    # merge or split stays reversible for the undo window.
    needs_review = [
        r
        for r in rows
        if {"possible_duplicate", "orphan_update", "addendum_gap", "date_conflict"}
        & set(r["o"].flags)
    ]
    return _templates().TemplateResponse(
        request,
        "opportunities.html",
        {
            "rows": rows,
            "needs_review": needs_review,
            "merges": curation.undoable(
                session, now=SystemClock().now(), window_days=settings.merge_undo_days
            ),
            "user": user,
            "status": status,
            "band": band,
        },
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
    sources = []
    for src in session.scalars(
        select(OpportunitySource)
        .where(OpportunitySource.opportunity_id == opp_id)
        .order_by(OpportunitySource.attached_at)
    ).all():
        sources.append({"src": src, "msg": session.get(RawMessage, src.message_id)})
    related = [
        r for r in (session.get(Opportunity, rid) for rid in o.related_project_ids) if r is not None
    ]
    return _templates().TemplateResponse(
        request,
        "opportunity.html",
        {
            "o": o,
            "score": score,
            "history": history,
            "decisions": decisions,
            "sources": sources,
            "addenda": session.scalars(
                select(Addendum)
                .where(Addendum.opportunity_id == opp_id)
                .order_by(Addendum.received_at)
            ).all(),
            "related": related,
            # Recomputed live rather than read off the `possible_duplicate` flag: that flag records
            # a message that attached provisionally (shown in Sources), which is a different
            # question from "is there another opportunity this one should be merged with".
            "duplicates": pipeline.possible_duplicates(session, o, limit=5),
            "user": user,
        },
    )
