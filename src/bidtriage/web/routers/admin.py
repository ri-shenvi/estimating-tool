from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.blobs import get_blob_store
from bidtriage.core.clock import aware
from bidtriage.core.config import get_settings
from bidtriage.core.crypto import sha256_hex
from bidtriage.core.models import GC, AuditEvent, ScoringProfile, Source
from bidtriage.ingestion.health import source_health
from bidtriage.ingestion.msg import parse_upload
from bidtriage.scoring.profile import Profile
from bidtriage.web.deps import CurrentUser, current_user, db, require_role
from bidtriage.worker import pipeline
from bidtriage.worker.ingest_job import (
    ingestion_metrics,
    latest_poll,
    pending_skips,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _templates():
    from bidtriage.web.app import templates

    return templates


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request, session: Session = Depends(db), user: CurrentUser = Depends(current_user)
):
    now = datetime.now(tz=UTC)
    sources = [
        {
            "row": s,
            "health": source_health(
                last_success_at=aware(s.last_success_at), now=now, paused=s.paused
            ),
            "last_poll": latest_poll(session, s.id),
            "skips": len(pending_skips(session, source_id=s.id)),
        }
        for s in session.scalars(select(Source).order_by(Source.name)).all()
    ]
    profiles = session.scalars(select(ScoringProfile).order_by(ScoringProfile.version.desc())).all()
    gcs = session.scalars(select(GC).order_by(GC.canonical_name)).all()
    return _templates().TemplateResponse(
        request,
        "admin.html",
        {
            "sources": sources,
            "profiles": profiles,
            "gcs": gcs,
            "user": user,
            "ingestion": ingestion_metrics(
                session,
                now=now,
                cache_seconds=get_settings().ingest_metrics_cache_seconds,
            ),
        },
    )


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks, refusing oversize input before the whole body is resident (F9)."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"file exceeds the {max_bytes} byte upload limit")
        chunks.append(chunk)
    if not total:
        raise HTTPException(400, "file is empty")
    return b"".join(chunks)


@router.post("/sources/{source_id}/pause")
def toggle_pause(
    source_id: str,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    src = session.get(Source, source_id)
    if src is None:
        raise HTTPException(404)
    src.paused = not src.paused
    session.add(
        AuditEvent(
            actor_user_id=None if user.id == "dev" else user.id,
            role=user.role,
            action="source.pause" if src.paused else "source.resume",
            entity_type="source",
            entity_id=src.id,
            before=None,
            after={"paused": src.paused},
            channel="admin",
            created_at=datetime.now(tz=UTC),
        )
    )
    return RedirectResponse("/admin/", status_code=303)


@router.post("/upload")
async def upload_message(
    file: UploadFile = File(...),
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief", "estimator")),
):
    """Manual upload of an `.eml` or `.msg` file (SPEC-01 F1). Ingests through the normal pipeline."""
    name = file.filename or "upload.eml"
    if not name.lower().endswith((".eml", ".msg")):
        raise HTTPException(400, "upload an .eml or .msg file")
    data = await _read_capped(file, get_settings().ingest_max_upload_bytes)
    try:
        parsed = parse_upload(name, data)
    except Exception as e:  # noqa: BLE001 - a malformed upload is a 400, not a 500
        raise HTTPException(400, f"could not parse {name}: {e}") from e
    src = session.scalar(select(Source).where(Source.kind == "manual"))
    if src is None:
        src = Source(
            kind="manual", name="Manual upload", status="active", backfill_done=True, paused=True
        )
        session.add(src)
        session.flush()
    provider_id = f"{name}:{sha256_hex(data)[:16]}"
    msg, is_new = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id=provider_id,
        parsed=parsed,
        recipient_path=user.email or "manual upload",
        blobs=get_blob_store(),
    )
    session.add(
        AuditEvent(
            actor_user_id=None if user.id == "dev" else user.id,
            role=user.role,
            action="message.upload",
            entity_type="raw_message",
            entity_id=msg.id,
            before=None,
            after={"filename": name, "new": is_new},
            channel="admin",
            created_at=datetime.now(tz=UTC),
        )
    )
    return RedirectResponse(f"/admin/?uploaded={'new' if is_new else 'duplicate'}", status_code=303)


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
