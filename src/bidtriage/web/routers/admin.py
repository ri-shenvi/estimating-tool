from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.models import GC, AuditEvent, ScoringProfile, Source
from bidtriage.scoring.profile import Profile
from bidtriage.web.deps import CurrentUser, current_user, db, require_role

router = APIRouter(prefix="/admin", tags=["admin"])


def _templates():
    from bidtriage.web.app import templates

    return templates


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request, session: Session = Depends(db), user: CurrentUser = Depends(current_user)
):
    sources = session.scalars(select(Source)).all()
    profiles = session.scalars(select(ScoringProfile).order_by(ScoringProfile.version.desc())).all()
    gcs = session.scalars(select(GC).order_by(GC.canonical_name)).all()
    return _templates().TemplateResponse(
        request, "admin.html", {"sources": sources, "profiles": profiles, "gcs": gcs, "user": user}
    )


@router.post("/gcs/{gc_id}/tier")
def set_tier(
    gc_id: str,
    tier: str = Form(...),
    reason: str = Form(default=""),
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    gc = session.get(GC, gc_id)
    if gc is None:
        raise HTTPException(404)
    if tier not in ("A", "B", "C", "D", "blocked", "unknown"):
        raise HTTPException(400, "bad tier")
    before = {"tier": gc.tier}
    gc.tier, gc.tier_reason, gc.tier_set_by, gc.tier_set_at = (
        tier,
        reason,
        user.id if user.id != "dev" else None,
        datetime.now(tz=UTC),
    )
    session.add(
        AuditEvent(
            actor_user_id=gc.tier_set_by,
            role=user.role,
            action="gc.tier",
            entity_type="gc",
            entity_id=gc.id,
            before=before,
            after={"tier": tier, "reason": reason},
            channel="admin",
            created_at=datetime.now(tz=UTC),
        )
    )
    from bidtriage.core.jobs import enqueue

    enqueue(
        session,
        "rescore_gc",
        f"rescore_gc:{gc.id}:{datetime.now(tz=UTC).isoformat()}",
        {"gc_id": gc.id},
    )
    return RedirectResponse("/admin/", status_code=303)


@router.post("/profiles")
def create_profile(
    json_body: str = Form(...),
    note: str = Form(default=""),
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    import json

    try:
        profile = Profile.model_validate(json.loads(json_body))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid profile: {e}") from e
    session.add(
        ScoringProfile(
            json=profile.model_dump(mode="json"),
            note=note,
            author_id=None if user.id == "dev" else user.id,
            active=False,
            created_at=datetime.now(tz=UTC),
        )
    )
    return RedirectResponse("/admin/", status_code=303)


@router.post("/profiles/{version}/activate")
def activate_profile(
    version: int,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    target = session.get(ScoringProfile, version)
    if target is None:
        raise HTTPException(404)
    for p in session.scalars(select(ScoringProfile).where(ScoringProfile.active.is_(True))).all():
        p.active = False
    target.active = True
    session.add(
        AuditEvent(
            actor_user_id=None if user.id == "dev" else user.id,
            role=user.role,
            action="profile.activate",
            entity_type="scoring_profile",
            entity_id=str(version),
            before=None,
            after={"version": version},
            channel="admin",
            created_at=datetime.now(tz=UTC),
        )
    )
    from bidtriage.core.jobs import enqueue

    enqueue(
        session,
        "rescore_all",
        f"rescore:{version}:{datetime.now(tz=UTC).date().isoformat()}",
        {"profile_version": version},
    )
    return RedirectResponse("/admin/", status_code=303)
