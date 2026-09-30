from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.models import Digest
from bidtriage.web.deps import CurrentUser, current_user, db

router = APIRouter(tags=["digests"])


@router.get("/digests/{date}", response_class=HTMLResponse)
def show(
    date: str, session: Session = Depends(db), user: CurrentUser = Depends(current_user)
) -> HTMLResponse:
    d = session.scalars(
        select(Digest).where(
            Digest.date == date, Digest.recipient_user_id == user.id, Digest.manual.is_(False)
        )
    ).first()
    if d is None:
        d = session.scalars(select(Digest).where(Digest.date == date)).first()
    if d is None:
        raise HTTPException(404, "no digest for that date")
    return HTMLResponse(d.html)
