"""One-click action links: GET shows a confirm page; POST applies (SPEC-06 F2, pre-fetch safe)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import SystemClock
from bidtriage.core.config import Settings, get_settings
from bidtriage.core.models import ActionTokenUsed, Opportunity, User
from bidtriage.decisions.actions import apply_action
from bidtriage.decisions.tokens import TokenError, verify_action
from bidtriage.web.deps import db

router = APIRouter(tags=["actions"])


def _templates():
    from bidtriage.web.app import templates

    return templates


def _verify(token: str, settings: Settings):
    try:
        return verify_action(
            token, settings.secret_key, ttl=timedelta(days=settings.action_token_ttl_days)
        )
    except TokenError as e:
        if str(e) == "expired":
            return None
        raise HTTPException(status_code=400, detail="invalid link") from e


@router.get("/a/{token}", response_class=HTMLResponse)
def confirm(
    token: str,
    request: Request,
    session: Session = Depends(db),
    settings: Settings = Depends(get_settings),
):
    t = _verify(token, settings)
    if t is None:
        return RedirectResponse(
            url="/opportunities/" + token.split(".")[1][:0] or "/", status_code=302
        )
    o = session.get(Opportunity, t.opportunity_id)
    if o is None:
        raise HTTPException(404)
    used = session.get(ActionTokenUsed, (t.opportunity_id, t.action, t.nonce))
    return _templates().TemplateResponse(
        request, "confirm.html", {"o": o, "t": t, "token": token, "used": used}
    )


@router.post("/a/{token}", response_class=HTMLResponse)
def apply(
    token: str,
    request: Request,
    session: Session = Depends(db),
    settings: Settings = Depends(get_settings),
    reason: str | None = Form(default=None),
    assignee: str | None = Form(default=None),
    days: int | None = Form(default=None),
):
    t = _verify(token, settings)
    if t is None:
        return RedirectResponse(url="/", status_code=302)
    o = session.get(Opportunity, t.opportunity_id)
    if o is None:
        raise HTTPException(404)
    if session.get(ActionTokenUsed, (t.opportunity_id, t.action, t.nonce)) is not None:
        return _templates().TemplateResponse(
            request, "confirm.html", {"o": o, "t": t, "token": token, "used": True}
        )
    user = session.get(User, t.recipient_id)
    payload: dict[str, object] = {}
    if t.action == "assign" and assignee:
        payload["assignee_user_id"] = assignee
    if t.action == "snooze" and days:
        payload["days"] = days
    apply_action(
        session,
        o,
        t.action,
        actor_user_id=t.recipient_id,
        actor_role=user.role if user else None,
        channel="digest",
        reason=reason,
        payload=payload,
        clock=SystemClock(),
    )
    session.add(
        ActionTokenUsed(
            opportunity_id=t.opportunity_id,
            action=t.action,
            nonce=t.nonce,
            used_at=SystemClock().now(),
        )
    )
    session.flush()
    return _templates().TemplateResponse(request, "done.html", {"o": o, "t": t})


@router.get("/users.json")
def users(session: Session = Depends(db)) -> list[dict[str, str]]:
    return [
        {"id": u.id, "name": u.name}
        for u in session.scalars(select(User).where(User.active.is_(True))).all()
    ]
