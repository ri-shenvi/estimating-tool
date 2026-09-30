"""Load rows -> DigestItems -> assemble -> render -> store -> send (SPEC-05)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import aware
from bidtriage.core.models import (
    Addendum,
    CalendarToken,
    Digest,
    DigestWatermark,
    Opportunity,
    RawMessage,
    Score,
    ScoringProfile,
    Source,
    User,
)
from bidtriage.decisions.tokens import sign_action
from bidtriage.digest.render import render_html, render_text, subject_line
from bidtriage.digest.sender import send_email
from bidtriage.digest.snapshot import DigestItem, HealthLine, ReviewItem, assemble
from bidtriage.ingestion.health import OK, source_health
from bidtriage.scoring.engine import ScoreResult


def _calendar_url(session: Session, user: User, base_url: str) -> str:
    tok = session.scalar(
        select(CalendarToken).where(
            CalendarToken.user_id == user.id, CalendarToken.revoked_at.is_(None)
        )
    )
    if tok is None:
        raw = secrets.token_urlsafe(24)
        session.add(
            CalendarToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                scope="personal",
            )
        )
        session.flush()
        return f"{base_url}/calendar/{raw}.ics"
    return f"{base_url}/calendar/(token-issued-earlier).ics"


def load_items(
    session: Session, *, recipient: User, base_url: str, secret_key: str, now: datetime
) -> list[DigestItem]:
    users = {u.id: u.name for u in session.scalars(select(User)).all()}
    items: list[DigestItem] = []
    for o in session.scalars(select(Opportunity).where(Opportunity.archived_at.is_(None))).all():
        c = o.canonical
        sc = session.scalars(
            select(Score).where(Score.opportunity_id == o.id).order_by(Score.computed_at.desc())
        ).first()
        res = ScoreResult.model_validate(sc.explanation) if sc else None
        addenda = session.scalars(select(Addendum).where(Addendum.opportunity_id == o.id)).all()
        docs = next(
            (
                lk.get("host_class")
                for lk in (c.get("document_links") or [])
                if lk.get("host_class") != "other"
            ),
            None,
        )
        due_raw = (c.get("bid_due") or {}).get("value")
        pb_raw = (c.get("prebid") or {}).get("value")
        rfi_raw = (c.get("rfi_deadline") or {}).get("value")
        loc = c.get("location") or {}
        gc_tier = "unknown"
        if o.gc_id:
            from bidtriage.core.models import GC

            g = session.get(GC, o.gc_id)
            gc_tier = g.tier if g else "unknown"
        actions = {}
        if recipient.role != "readonly":
            for label, action in (
                ("Bid", "bid"),
                ("Pass", "pass"),
                ("Assign", "assign"),
                ("Snooze 3d", "snooze"),
            ):
                actions[label] = (
                    f"{base_url}/a/{sign_action(o.id, action, recipient.id, secret_key)}"
                )
        items.append(
            DigestItem(
                opportunity_id=o.id,
                project_name=(c.get("project_name") or {}).get("value") or "Untitled",
                gc_name=(c.get("gc_name") or {}).get("value"),
                gc_tier=gc_tier,
                project_type=c.get("project_type", "unknown"),
                new_or_renovation=c.get("new_or_renovation", "unknown"),
                location_short=(
                    f"{loc.get('city')}, {loc.get('state')}"
                    if loc.get("city") and loc.get("state")
                    else loc.get("city") or loc.get("raw")
                ),
                distance_miles=_distance_from(res),
                size_reason=res.size_estimate.reason if res else "size unknown",
                bid_due=datetime.fromisoformat(due_raw) if due_raw else None,
                bid_due_time_known=bool((c.get("bid_due") or {}).get("time_known")),
                prebid_at=datetime.fromisoformat(pb_raw) if pb_raw else None,
                prebid_mandatory=(c.get("prebid") or {}).get("mandatory"),
                rfi_deadline=datetime.fromisoformat(rfi_raw) if rfi_raw else None,
                summary=c.get("summary", ""),
                exclusions=c.get("exclusions_text"),
                addenda_count=len(addenda),
                addendum_gap="addendum_gap" in o.flags,
                docs_host=docs,
                score=res.score if res else 0,
                band=res.band if res else "pass",
                why_positive=[x.reason for x in res.top_positive(3)] if res else [],
                why_negative=[x.reason for x in res.top_negative(1)]
                + [w for w in (res.warnings if res else [])][:1]
                if res
                else [],
                flags=[
                    f
                    for f in o.flags
                    if f
                    in (
                        "prevailing_wage",
                        "davis_bacon",
                        "bid_bond",
                        "pp_bond",
                        "mandatory_prebid",
                        "open_shop_indicated",
                        "sealed_bid",
                        "date_conflict",
                        "possible_duplicate",
                        "orphan_update",
                    )
                ],
                status=o.status,
                assignee_id=o.assignee_user_id,
                assignee_name=users.get(o.assignee_user_id or "", None),
                first_seen_at=aware(o.first_seen_at),
                changed_since_digest=o.changed_since_digest,
                change_summary=o.change_summary,
                actions=actions,
                open_url=f"{base_url}/opportunities/{o.id}",
            )
        )
    return items


def _distance_from(res: ScoreResult | None) -> float | None:
    if res is None:
        return None
    d = next((c for c in res.contributions if c.factor == "distance"), None)
    if d is None or d.reason == "distance unknown":
        return None
    try:
        return float(d.reason.split()[0])
    except ValueError:
        return None


def health_lines(session: Session, now: datetime) -> list[HealthLine]:
    """System health section of the digest, using the SPEC-01 F8 thresholds."""
    out = []
    for s in session.scalars(select(Source)).all():
        h = source_health(last_success_at=aware(s.last_success_at), now=now, paused=s.paused)
        out.append(
            HealthLine(name=s.name, status=h.status, detail="" if h.status == OK else h.detail)
        )
    return out


def review_items(session: Session, base_url: str) -> list[ReviewItem]:
    out = []
    for m in session.scalars(
        select(RawMessage).where(RawMessage.extraction_status == "failed")
    ).all():
        out.append(
            ReviewItem(
                kind="extraction_failed",
                title=f"{m.subject[:70]} — {m.from_addr}",
                url=f"{base_url}/admin/",
            )
        )
    for m in session.scalars(
        select(RawMessage).where(
            RawMessage.kind_confidence < 0.6, RawMessage.extraction_status == "done"
        )
    ).all():
        out.append(
            ReviewItem(
                kind="low_confidence_kind",
                title=f"{m.subject[:70]} ({m.kind})",
                url=f"{base_url}/admin/",
            )
        )
    for o in session.scalars(select(Opportunity).where(Opportunity.archived_at.is_(None))).all():
        if "possible_duplicate" in o.flags:
            out.append(
                ReviewItem(
                    kind="possible_duplicate",
                    title=(o.canonical.get("project_name") or {}).get("value") or o.id,
                    url=f"{base_url}/opportunities/{o.id}",
                )
            )
        if "orphan_update" in o.flags:
            out.append(
                ReviewItem(
                    kind="orphan_update",
                    title=(o.canonical.get("project_name") or {}).get("value") or o.id,
                    url=f"{base_url}/opportunities/{o.id}",
                )
            )
    return out


def build_and_send(
    session: Session, ctx, *, date: str | None, recipient_id: str | None, send: bool = True
) -> list[Digest]:
    settings = ctx.settings
    tz = ZoneInfo(settings.digest_timezone)
    now = datetime.now(tz=tz)
    recipients = (
        [session.get(User, recipient_id)]
        if recipient_id
        else session.scalars(select(User).where(User.active.is_(True))).all()
    )
    profile = session.scalar(select(ScoringProfile).where(ScoringProfile.active.is_(True)))
    built: list[Digest] = []
    for user in recipients:
        if user is None:
            continue
        wm = session.get(DigestWatermark, user.id)
        items = load_items(
            session,
            recipient=user,
            base_url=settings.base_url,
            secret_key=settings.secret_key,
            now=now,
        )
        snap = assemble(
            items=items,
            recipient_id=user.id,
            recipient_name=user.name,
            recipient_role=user.role,
            now=now,
            since=wm.last_scheduled_digest_at if wm else None,
            min_band=(user.digest_prefs or {}).get("min_band", "pass"),
            review=review_items(session, settings.base_url)
            if user.role in ("chief", "admin")
            else [],
            health=health_lines(session, datetime.now(tz=UTC)),
            review_url=f"{settings.base_url}/",
            calendar_url=_calendar_url(session, user, settings.base_url),
            profile_version=profile.version if profile else 0,
        )
        html, text = render_html(snap), render_text(snap)
        d = Digest(
            date=date or now.date().isoformat(),
            recipient_user_id=user.id,
            manual=not send,
            snapshot=snap.model_dump(mode="json"),
            html=html,
            text=text,
            status="built",
        )
        session.add(d)
        if send:
            mid = send_email(
                smtp_url=settings.smtp_url,
                sender=settings.digest_from,
                to=user.email,
                subject=subject_line(snap),
                html=html,
                text=text,
            )
            d.sent_at, d.provider_message_id, d.status = datetime.now(tz=UTC), mid, "sent"
            if wm is None:
                session.add(
                    DigestWatermark(
                        recipient_user_id=user.id, last_scheduled_digest_at=datetime.now(tz=UTC)
                    )
                )
            else:
                wm.last_scheduled_digest_at = datetime.now(tz=UTC)
        built.append(d)
    if send:
        for o in session.scalars(
            select(Opportunity).where(Opportunity.changed_since_digest.is_(True))
        ).all():
            o.changed_since_digest = False
    session.flush()
    return built
