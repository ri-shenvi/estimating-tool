from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.calendar.ics import CalendarEvent, build_ics
from bidtriage.core.config import Settings, get_settings
from bidtriage.core.models import CalendarToken, Opportunity
from bidtriage.web.deps import db

router = APIRouter(tags=["calendar"])


def events_for(opps: list[Opportunity], base_url: str) -> list[CalendarEvent]:
    out: list[CalendarEvent] = []
    for o in opps:
        c = o.canonical
        name = (c.get("project_name") or {}).get("value") or "Untitled"
        gc = (c.get("gc_name") or {}).get("value") or "Unknown GC"
        cancelled = o.status in ("passed", "lost", "cancelled", "archived")
        desc = f"Status: {o.status}\nScope: {c.get('summary', '')}\n{base_url}/opportunities/{o.id}"
        bd = (c.get("bid_due") or {}).get("value")
        if bd:
            dt = datetime.fromisoformat(bd)
            time_known = (c.get("bid_due") or {}).get("time_known", False)
            title = f"{'SUBMITTED' if o.status == 'submitted' else 'BID DUE'}: {name} ({gc})"
            out.append(
                CalendarEvent(
                    uid=f"{o.id}-due@bidtriage",
                    summary=title,
                    start=dt - timedelta(minutes=30) if time_known else dt.date(),
                    duration=timedelta(minutes=30) if time_known else None,
                    description=desc,
                    sequence=len(o.locked_fields) + 0,
                    cancelled=cancelled,
                    alarms_before=[timedelta(days=1), timedelta(hours=2)]
                    if time_known
                    else [timedelta(days=1)],
                )
            )
        pb = (c.get("prebid") or {}).get("value")
        if pb and o.status != "submitted":
            dt = datetime.fromisoformat(pb)
            mand = (c.get("prebid") or {}).get("mandatory")
            out.append(
                CalendarEvent(
                    uid=f"{o.id}-prebid@bidtriage",
                    summary=f"PRE-BID{' (MANDATORY)' if mand else ''}: {name} ({gc})",
                    start=dt,
                    duration=timedelta(minutes=90),
                    description=desc,
                    location=(c.get("prebid") or {}).get("location"),
                    cancelled=cancelled,
                    alarms_before=[timedelta(days=1)],
                )
            )
        rfi = (c.get("rfi_deadline") or {}).get("value")
        if rfi and not cancelled:
            out.append(
                CalendarEvent(
                    uid=f"{o.id}-rfi@bidtriage",
                    summary=f"RFIs DUE: {name}",
                    start=datetime.fromisoformat(rfi).date(),
                    duration=None,
                    description=desc,
                    alarms_before=[timedelta(days=1)],
                )
            )
    return out


@router.get("/calendar/{token}.ics")
def feed(
    token: str,
    session: Session = Depends(db),
    settings: Settings = Depends(get_settings),
    status: str | None = None,
) -> Response:
    h = hashlib.sha256(token.encode()).hexdigest()
    ct = session.scalar(
        select(CalendarToken).where(
            CalendarToken.token_hash == h, CalendarToken.revoked_at.is_(None)
        )
    )
    if ct is None:
        raise HTTPException(404)
    statuses = (
        status.split(",")
        if status
        else ["bidding", "undecided", "submitted", "passed", "lost", "cancelled"]
    )
    cutoff = datetime.now(tz=UTC) - timedelta(days=7)
    opps = [
        o
        for o in session.scalars(select(Opportunity).where(Opportunity.status.in_(statuses))).all()
        if o.status in ("bidding", "undecided", "submitted") or o.last_activity_at >= cutoff
    ]
    if ct.scope == "personal" and ct.user_id:
        opps = [
            o
            for o in opps
            if o.assignee_user_id == ct.user_id
            or (o.assignee_user_id is None and o.status == "undecided")
        ]
    body = build_ics(events_for(opps, settings.base_url))
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "max-age=300"},
    )
