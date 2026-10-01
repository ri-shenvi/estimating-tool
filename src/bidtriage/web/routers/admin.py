from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.blobs import get_blob_store
from bidtriage.core.clock import aware
from bidtriage.core.config import get_settings
from bidtriage.core.crypto import sha256_hex
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import GC, AuditEvent, RawMessage, ScoringProfile, Source
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
    review = [
        {
            "msg": m,
            "extraction": pipeline.latest_extraction(session, m.id),
        }
        for m in pipeline.messages_needing_review(session)
    ]
    profiles = session.scalars(select(ScoringProfile).order_by(ScoringProfile.version.desc())).all()
    gcs = session.scalars(select(GC).order_by(GC.canonical_name)).all()
    return _templates().TemplateResponse(
        request,
        "admin.html",
        {
            "sources": sources,
            "review": review,
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


@router.post("/messages/{message_id}/reextract")
def reextract_message(
    message_id: str,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief", "estimator")),
):
    """Queue a fresh extraction for one message (SPEC-02 F4).

    Queued rather than run inline: it is an LLM call, and the estimator should not wait on it. The
    existing record stays until the new one lands, then points at it via `superseded_by`.
    """
    msg = session.get(RawMessage, message_id)
    if msg is None:
        raise HTTPException(404)
    now = datetime.now(tz=UTC)
    enqueue(
        session,
        "reextract_message",
        f"reextract:{msg.id}:manual:{now.isoformat()}",
        {"message_id": msg.id},
        priority=60,
    )
    session.add(
        AuditEvent(
            actor_user_id=None if user.id == "dev" else user.id,
            role=user.role,
            action="message.reextract",
            entity_type="raw_message",
            entity_id=msg.id,
            before={"kind": msg.kind, "status": msg.extraction_status},
            after=None,
            channel="admin",
            created_at=now,
        )
    )
    return RedirectResponse("/admin/?reextract=queued#review", status_code=303)


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


# ------------------------------------------------------- scoring profiles (SPEC-04 F7)


def _coerce(current: Any, raw: str) -> Any:
    """Parse one form value back into the shape the profile JSON already has at that path."""
    raw = raw.strip()
    if isinstance(current, bool):
        return raw.lower() in ("1", "true", "on", "yes")
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw or None


def profile_from_form(base: Profile, form: Mapping[str, Any]) -> Profile:
    """Rebuild a profile from `p.<dotted.path>` form fields laid over an existing version.

    The editor is a thin form over the published JSON schema (SPEC-04 technical notes): the
    template renders one input per leaf, and this walks them back into place. Validation is the
    model's, so a weights column that sums to 1.05 is refused here exactly as it is in the API.
    """
    data = base.model_dump(mode="json")
    for key, raw in form.items():
        if not key.startswith("p.") or not isinstance(raw, str):
            continue
        *parents, leaf = key[2:].split(".")
        node: Any = data
        for part in parents:
            if not isinstance(node, dict):
                raise HTTPException(400, f"unknown profile field {key}")
            node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise HTTPException(400, f"unknown profile field {key}")
        node[leaf] = _coerce(node.get(leaf), raw)
    try:
        return Profile.model_validate(data)
    except ValidationError as e:
        raise HTTPException(400, f"invalid profile: {_first_error(e)}") from e


def _first_error(e: ValidationError) -> str:
    first = e.errors()[0]
    return str(first.get("msg", "")).removeprefix("Value error, ")


def _based_on(form: Mapping[str, Any]) -> int | None:
    """The version this edit started from, as the hidden field carries it."""
    raw = form.get("based_on")
    return int(raw) if isinstance(raw, str) and raw.strip() else None


def _base_profile(session: Session, version: int | None) -> tuple[Profile, int | None]:
    if version is None:
        return pipeline.active_profile(session)
    row = session.get(ScoringProfile, version)
    if row is None:
        raise HTTPException(404)
    return Profile.model_validate(row.json), row.version


def _edit_page(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    profile: Profile,
    based_on: int | None,
    note: str = "",
    preview: pipeline.ProfilePreview | None = None,
    error: str | None = None,
):
    return _templates().TemplateResponse(
        request,
        "profile_edit.html",
        {
            "user": user,
            "profile": profile.model_dump(mode="json"),
            "based_on": based_on,
            "note": note,
            "preview": preview,
            "error": error,
            "warnings": profile.validation_warnings(),
            "versions": session.scalars(
                select(ScoringProfile).order_by(ScoringProfile.version.desc())
            ).all(),
        },
    )


@router.get("/profiles/edit", response_class=HTMLResponse)
def edit_profile(
    request: Request,
    version: int | None = None,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    """The profile form, started from an existing version (default: the active one)."""
    profile, based_on = _base_profile(session, version)
    return _edit_page(request, session, user, profile=profile, based_on=based_on)


@router.get("/profiles/schema.json")
def profile_schema() -> dict[str, Any]:
    """The published profile JSON schema the editor and any external tooling validate against."""
    return Profile.model_json_schema()


@router.post("/profiles/preview", response_class=HTMLResponse)
async def preview_profile(
    request: Request,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    """Rescore the last 30 days under the edited profile and show what would move. Writes nothing."""
    form = await request.form()
    based_on = _based_on(form)
    note = str(form.get("note") or "")
    base, _ = _base_profile(session, based_on)
    try:
        candidate = profile_from_form(base, form)
    except HTTPException as e:
        return _edit_page(
            request,
            session,
            user,
            profile=base,
            based_on=based_on,
            note=note,
            error=str(e.detail),
        )
    settings = get_settings()
    preview = pipeline.preview_profile(
        session,
        candidate,
        now=datetime.now(tz=UTC),
        home=(settings.home_lat, settings.home_lon),
    )
    return _edit_page(
        request,
        session,
        user,
        profile=candidate,
        based_on=based_on,
        note=note,
        preview=preview,
    )


@router.post("/profiles")
async def create_profile(
    request: Request,
    session: Session = Depends(db),
    user: CurrentUser = Depends(require_role("admin", "chief")),
):
    """Save a draft version. Drafts are inert until someone activates them."""
    form = await request.form()
    raw_json = form.get("json_body")
    if isinstance(raw_json, str) and raw_json.strip():
        import json

        try:
            profile = Profile.model_validate(json.loads(raw_json))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"invalid profile: {e}") from e
        based_on = None
    else:
        based_on = _based_on(form)
        base, _ = _base_profile(session, based_on)
        profile = profile_from_form(base, form)
    note = str(form.get("note") or "")
    row = ScoringProfile(
        json=profile.model_dump(mode="json"),
        note=note,
        author_id=None if user.id == "dev" else user.id,
        active=False,
        created_at=datetime.now(tz=UTC),
    )
    session.add(row)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=None if user.id == "dev" else user.id,
            role=user.role,
            action="profile.create",
            entity_type="scoring_profile",
            entity_id=str(row.version),
            before=None,
            after={"version": row.version, "based_on": based_on, "note": note},
            channel="admin",
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
