"""Pipeline glue: parsed message -> stored -> extracted -> resolved -> scored.

Each step is a function over a Session so the worker, the CLI and tests share one code path.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.clock import Clock, SystemClock, aware
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import (
    GC,
    Addendum,
    Extraction,
    FieldHistory,
    GCStats,
    MessageLink,
    MessageSource,
    Opportunity,
    OpportunitySource,
    RawAttachment,
    RawMessage,
    Score,
    ScoringProfile,
)
from bidtriage.extraction.postprocess import PLATFORM_DOMAINS
from bidtriage.extraction.prefilter import obviously_not_bid
from bidtriage.extraction.protocol import AttachmentText, ExtractionInput, Extractor
from bidtriage.extraction.schema import EXTRACTABLE_KINDS, ExtractedOpportunity, Kind
from bidtriage.gcs.resolve import GCRecord, resolve_gc
from bidtriage.ingestion.attachments import extract_text, sanitize_filename
from bidtriage.ingestion.eml import ParsedMessage
from bidtriage.ingestion.links import harvest_links
from bidtriage.resolution.evidence import Candidate, Incoming, decide, evidence
from bidtriage.resolution.merge import addendum_gaps, merge_date
from bidtriage.resolution.normalize import fingerprint, normalize_domain, normalize_name
from bidtriage.scoring.engine import ScoreResult, score
from bidtriage.scoring.profile import DEFAULT_PROFILE, Profile
from bidtriage.scoring.snapshot import CalendarSnapshot, GCSnapshot, OpportunitySnapshot

# ---------------------------------------------------------------- ingestion


def ingest_parsed(
    session: Session,
    *,
    source_id: str,
    provider_message_id: str,
    parsed: ParsedMessage,
    recipient_path: str = "",
    clock: Clock | None = None,
    ocr: bool = False,
) -> tuple[RawMessage, bool]:
    """Store a parsed message idempotently (SPEC-01 F3). Returns (message, is_new)."""
    clock = clock or SystemClock()
    now = clock.now()
    existing_link = session.scalar(
        select(MessageSource).where(
            MessageSource.source_id == source_id,
            MessageSource.provider_message_id == provider_message_id,
        )
    )
    if existing_link is not None:
        msg = session.get(RawMessage, existing_link.message_id)
        assert msg is not None
        return msg, False

    dupe: RawMessage | None = None
    if parsed.internet_message_id:
        dupe = session.scalar(
            select(RawMessage).where(RawMessage.internet_message_id == parsed.internet_message_id)
        )
    if dupe is None:
        window = now - timedelta(days=7)
        dupe = session.scalar(
            select(RawMessage).where(
                RawMessage.content_hash == parsed.content_hash, RawMessage.received_at >= window
            )
        )
    if dupe is not None:
        dupe.copies += 1
        session.add(
            MessageSource(
                message_id=dupe.id,
                source_id=source_id,
                provider_message_id=provider_message_id,
                recipient_path=recipient_path,
            )
        )
        session.flush()
        return dupe, False

    msg = RawMessage(
        internet_message_id=parsed.internet_message_id,
        content_hash=parsed.content_hash,
        from_addr=parsed.from_addr,
        from_name=parsed.from_name,
        to=parsed.to,
        cc=parsed.cc,
        subject=parsed.subject,
        sent_at=parsed.sent_at,
        received_at=now,
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        body_trimmed=parsed.body_trimmed,
        headers=parsed.headers,
        in_reply_to=parsed.in_reply_to,
        references=parsed.references,
        forward_note=parsed.forward_note,
        created_at=now,
    )
    session.add(msg)
    session.flush()
    session.add(
        MessageSource(
            message_id=msg.id,
            source_id=source_id,
            provider_message_id=provider_message_id,
            recipient_path=recipient_path,
        )
    )
    for a in parsed.attachments:
        ext = extract_text(a.filename, a.mime, a.data, ocr=ocr)
        session.add(
            RawAttachment(
                message_id=msg.id,
                filename=sanitize_filename(a.filename),
                mime=a.mime,
                size=a.size,
                sha256=a.sha256,
                text=ext.text,
                ocr=ext.ocr,
                large_document=ext.large_document,
                extraction_error=ext.error,
            )
        )
    texts = [parsed.body_text, parsed.body_html]
    for link in harvest_links(*texts):
        session.add(
            MessageLink(
                message_id=msg.id,
                url=str(link["url"]),
                host_class=str(link["host_class"]),
                wrapped=bool(link["wrapped"]),
            )
        )
    session.flush()
    enqueue(session, "extract_message", f"extract:{msg.id}:v1", {"message_id": msg.id}, clock=clock)
    return msg, True


# ---------------------------------------------------------------- extraction


def build_extraction_input(
    session: Session, msg: RawMessage, *, external_ref: str | None = None
) -> ExtractionInput:
    atts = [
        AttachmentText(filename=a.filename, text=a.text or "", large_document=a.large_document)
        for a in session.scalars(
            select(RawAttachment).where(RawAttachment.message_id == msg.id)
        ).all()
    ]
    links = [
        lk.url
        for lk in session.scalars(select(MessageLink).where(MessageLink.message_id == msg.id)).all()
    ]
    return ExtractionInput(
        message_id=msg.id,
        subject=msg.subject,
        from_addr=msg.from_addr,
        from_name=msg.from_name,
        to=[str(t) for t in msg.to],
        sent_at=msg.sent_at or msg.received_at,
        body=msg.body_trimmed or msg.body_text,
        attachments=atts,
        links=links,
        external_ref=external_ref,
    )


def extract_message(
    session: Session,
    msg: RawMessage,
    extractor: Extractor,
    *,
    external_ref: str | None = None,
    clock: Clock | None = None,
) -> Extraction | None:
    clock = clock or SystemClock()
    item = build_extraction_input(session, msg, external_ref=external_ref)
    if obviously_not_bid(item):
        msg.kind, msg.kind_confidence, msg.extraction_status = Kind.not_bid.value, 0.99, "skipped"
        session.flush()
        return None
    try:
        result = extractor.extract(item)
    except Exception as e:  # noqa: BLE001
        msg.extraction_status = "failed"
        session.flush()
        raise e
    msg.kind, msg.kind_confidence, msg.extraction_status = (
        result.kind.value,
        result.kind_confidence,
        "done",
    )
    ext = Extraction(
        message_id=msg.id,
        model=result.extraction_meta.model,
        prompt_version=result.extraction_meta.prompt_version,
        payload=result.model_dump(mode="json"),
        tokens_in=result.extraction_meta.input_tokens,
        tokens_out=result.extraction_meta.output_tokens,
        latency_ms=result.extraction_meta.latency_ms,
        created_at=clock.now(),
    )
    session.add(ext)
    session.flush()
    if result.kind in EXTRACTABLE_KINDS:
        enqueue(
            session,
            "resolve_message",
            f"resolve:{msg.id}:{ext.id}",
            {"message_id": msg.id, "extraction_id": ext.id},
            clock=clock,
        )
    return ext


# ---------------------------------------------------------------- resolution


def _gc_records(session: Session) -> list[GCRecord]:
    return [
        GCRecord(
            id=g.id,
            canonical_name=g.canonical_name,
            aliases={a.alias for a in g.aliases},
            domains={d.domain for d in g.domains},
            tier=g.tier,
        )
        for g in session.scalars(select(GC)).all()
    ]


def _platform_ids(links: list[str]) -> set[str]:
    import re

    ids: set[str] = set()
    for u in links:
        m = (
            re.search(r"buildingconnected\.com/(?:projects|bids|rfp)/([0-9a-f]{12,})", u, re.I)
            or re.search(r"procore\.com/(\d+)/", u)
            or re.search(r"[?&](?:bidPackageId|projectId|project_id)=([\w-]+)", u)
        )
        if m:
            ids.add(m.group(1).lower())
    return ids


def _thread_ids(msg: RawMessage) -> set[str]:
    ids = (
        {str(r) for r in msg.references}
        | ({msg.in_reply_to} if msg.in_reply_to else set())
        | ({msg.internet_message_id} if msg.internet_message_id else set())
    )
    return {i for i in ids if i}


def _short_location(loc: dict[str, Any]) -> str | None:
    city, state = loc.get("city"), loc.get("state")
    if city and state:
        return f"{city}, {state}"
    return city or loc.get("raw")


def resolve_message(
    session: Session,
    msg: RawMessage,
    ext: Extraction,
    *,
    clock: Clock | None = None,
    geocoder: Callable[[str], tuple[float, float] | None] | None = None,
) -> tuple[Opportunity, str]:
    """Attach a message to an opportunity (SPEC-03). Returns (opportunity, decision)."""
    clock = clock or SystemClock()
    now = clock.now()
    x = ExtractedOpportunity.model_validate(ext.payload)
    links = [
        lk.url
        for lk in session.scalars(select(MessageLink).where(MessageLink.message_id == msg.id)).all()
    ]
    contact_domains = [c.email for c in x.gc_contacts if c.email] + (
        [msg.from_addr]
        if msg.from_addr and not any(msg.from_addr.endswith(p) for p in PLATFORM_DOMAINS)
        else []
    )
    gc_match = resolve_gc(x.gc_name.value, contact_domains, _gc_records(session))
    gc_id = gc_match.gc.id if gc_match.gc else None
    if gc_id is None and x.gc_name.value:
        gc = GC(
            canonical_name=x.gc_name.value, kind="gc", created_from="extraction", created_at=now
        )
        session.add(gc)
        session.flush()
        gc_id = gc.id
    gc_domain = next(
        (
            normalize_domain(d)
            for d in contact_domains
            if normalize_domain(d) not in ("gmail.com", "outlook.com", "yahoo.com")
        ),
        None,
    )

    lat = lon = None
    if geocoder is not None and x.location.raw:
        geo = geocoder(x.location.raw)
        if geo:
            lat, lon = geo
    incoming = Incoming(
        project_name=x.project_name.value,
        gc_id=gc_id,
        gc_domain=gc_domain,
        city=x.location.city,
        lat=lat,
        lon=lon,
        bid_due=x.bid_due.value,
        owner_name=x.owner_name.value,
        project_number=x.project_number.value,
        platform_ids=_platform_ids(links),
        thread_ids=_thread_ids(msg),
        kind=x.kind.value,
    )

    # candidates: every non-archived opportunity (small volume). Postgres could prefilter with pg_trgm.
    best: tuple[float, Opportunity | None, Any] = (-1.0, None, None)
    for o in session.scalars(select(Opportunity).where(Opportunity.archived_at.is_(None))).all():
        c = o.canonical
        srcs = session.scalars(
            select(OpportunitySource).where(OpportunitySource.opportunity_id == o.id)
        ).all()
        thread: set[str] = set()
        pids: set[str] = set()
        for s in srcs:
            m = session.get(RawMessage, s.message_id)
            if m:
                thread |= _thread_ids(m)
                pids |= _platform_ids(
                    [
                        lk.url
                        for lk in session.scalars(
                            select(MessageLink).where(MessageLink.message_id == m.id)
                        ).all()
                    ]
                )
        cand = Candidate(
            opportunity_id=o.id,
            project_name=(c.get("project_name") or {}).get("value"),
            gc_id=o.gc_id,
            gc_domain=c.get("gc_domain"),
            city=(c.get("location") or {}).get("city"),
            lat=o.lat,
            lon=o.lon,
            bid_due=datetime.fromisoformat(c["bid_due"]["value"])
            if (c.get("bid_due") or {}).get("value")
            else None,
            owner_name=(c.get("owner_name") or {}).get("value"),
            project_number=(c.get("project_number") or {}).get("value"),
            platform_ids=pids,
            thread_ids=thread,
        )
        ev = evidence(cand, incoming)
        if ev.score > best[0]:
            best = (ev.score, o, ev)

    decision = "new"
    if best[1] is not None:
        decision = decide(best[2], incoming_kind=x.kind.value, candidate_same_gc=best[2].same_gc)

    if decision in ("merge", "review") and best[1] is not None:
        opp = best[1]
        _apply_update(session, opp, x, msg, now, provisional=(decision == "review"))
        session.add(
            OpportunitySource(
                opportunity_id=opp.id,
                message_id=msg.id,
                role=x.kind.value,
                attached_at=now,
                evidence={
                    "score": best[2].score,
                    "hard_key": best[2].hard_key,
                    "components": best[2].components,
                    "provisional": decision == "review",
                },
            )
        )
    else:
        opp = Opportunity(
            status="new",
            gc_id=gc_id,
            canonical=_canonical_from(x, gc_domain, aware(msg.sent_at) or now),
            normalized_name=normalize_name(x.project_name.value),
            fingerprint=fingerprint(
                x.project_name.value, gc_domain, x.location.city, x.bid_due.value
            ),
            lat=lat,
            lon=lon,
            first_seen_at=now,
            last_activity_at=now,
            flags=[f.value for f in x.flags],
        )
        if x.kind != Kind.itb and x.kind != Kind.rfb:
            opp.flags = [*opp.flags, "orphan_update"]
        session.add(opp)
        session.flush()
        session.add(
            OpportunitySource(
                opportunity_id=opp.id,
                message_id=msg.id,
                role="origin",
                attached_at=now,
                evidence={},
            )
        )
        if decision == "related" and best[1] is not None:
            opp.related_project_ids = [best[1].id]
            best[1].related_project_ids = [*best[1].related_project_ids, opp.id]
        if x.kind == Kind.addendum:
            _record_addendum(session, opp, x, msg, now)
    session.flush()
    enqueue(
        session,
        "score_opportunity",
        f"score:{opp.id}:{ext.id}",
        {"opportunity_id": opp.id},
        clock=clock,
    )
    return opp, decision


def _canonical_from(
    x: ExtractedOpportunity, gc_domain: str | None, as_of: datetime | None = None
) -> dict[str, Any]:
    c = x.model_dump(mode="json")
    c["gc_domain"] = gc_domain
    if as_of is not None:
        for field in ("bid_due", "rfi_deadline", "intent_due"):
            if (c.get(field) or {}).get("value"):
                c[field]["as_of"] = as_of.isoformat()
    return c


def _record_addendum(
    session: Session, opp: Opportunity, x: ExtractedOpportunity, msg: RawMessage, now: datetime
) -> None:
    label = x.addendum_label or (
        f"Addendum {x.addendum_number}" if x.addendum_number else f"Addendum ({msg.subject[:30]})"
    )
    if (
        session.scalar(
            select(Addendum).where(Addendum.opportunity_id == opp.id, Addendum.label == label)
        )
        is None
    ):
        session.add(
            Addendum(
                opportunity_id=opp.id,
                label=label,
                number=x.addendum_number,
                message_id=msg.id,
                received_at=now,
                summary=x.changes_described or x.summary,
            )
        )
        session.flush()
    nums = [
        a.number
        for a in session.scalars(select(Addendum).where(Addendum.opportunity_id == opp.id)).all()
        if a.number is not None
    ]
    if addendum_gaps(nums):
        opp.flags = list({*opp.flags, "addendum_gap"})
    elif "addendum_gap" in opp.flags:
        opp.flags = [f for f in opp.flags if f != "addendum_gap"]


def _apply_update(
    session: Session,
    opp: Opportunity,
    x: ExtractedOpportunity,
    msg: RawMessage,
    now: datetime,
    *,
    provisional: bool,
) -> None:
    c = dict(opp.canonical)
    changes: list[str] = []
    for field in ("bid_due", "rfi_deadline", "intent_due"):
        existing_raw = (c.get(field) or {}).get("value")
        existing = datetime.fromisoformat(existing_raw) if existing_raw else None
        incoming = getattr(x, field).value
        set_by_raw = (c.get(field) or {}).get("as_of")
        set_by = aware(datetime.fromisoformat(set_by_raw)) if set_by_raw else None
        msg_sent = aware(msg.sent_at)
        if (
            existing is not None
            and incoming is not None
            and set_by
            and msg_sent
            and msg_sent < set_by
            and x.kind.value in ("itb", "addendum", "date_change")
        ):
            session.add(
                FieldHistory(
                    opportunity_id=opp.id,
                    field=field,
                    old={"value": existing_raw},
                    new={"value": incoming.isoformat()},
                    message_id=msg.id,
                    applied=False,
                    changed_at=now,
                )
            )
            continue  # older message must not move a newer date backwards
        out = merge_date(
            existing,
            incoming,
            incoming_kind=x.kind.value,
            locked=field in opp.locked_fields,
            field=field,
        )
        if out.changed or out.conflict:
            session.add(
                FieldHistory(
                    opportunity_id=opp.id,
                    field=field,
                    old={"value": existing_raw},
                    new={"value": incoming.isoformat() if incoming else None},
                    message_id=msg.id,
                    applied=out.changed,
                    changed_at=now,
                )
            )
        if out.changed:
            c[field] = {
                **getattr(x, field).model_dump(mode="json"),
                "as_of": (aware(msg.sent_at) or now).isoformat(),
            }
            changes.append(out.note + (f" ({x.addendum_label})" if x.addendum_label else ""))
        if out.conflict:
            opp.flags = list({*opp.flags, "date_conflict"})
            changes.append(out.note)
    # pre-bid
    existing_pb = (c.get("prebid") or {}).get("value")
    if (
        x.prebid.value
        and (not existing_pb or datetime.fromisoformat(existing_pb) != x.prebid.value)
        and x.kind.value in ("itb", "addendum", "date_change", "prebid_notice")
    ):
        session.add(
            FieldHistory(
                opportunity_id=opp.id,
                field="prebid",
                old={"value": existing_pb},
                new={"value": x.prebid.value.isoformat()},
                message_id=msg.id,
                applied=True,
                changed_at=now,
            )
        )
        c["prebid"] = x.prebid.model_dump(mode="json")
        changes.append(
            f"pre-bid {'set' if not existing_pb else 'moved'} to {x.prebid.value:%b %d %I:%M %p}"
        )
    # scope and flags: union
    new_scope = sorted(set(c.get("scope_items") or []) | {s.value for s in x.scope_items})
    if new_scope != sorted(c.get("scope_items") or []):
        added = set(new_scope) - set(c.get("scope_items") or [])
        session.add(
            FieldHistory(
                opportunity_id=opp.id,
                field="scope_items",
                old={"value": c.get("scope_items")},
                new={"value": new_scope},
                message_id=msg.id,
                applied=True,
                changed_at=now,
            )
        )
        c["scope_items"] = new_scope
        changes.append("scope adds " + ", ".join(sorted(a.replace("_", " ") for a in added)))
    opp.flags = sorted(set(opp.flags) | {f.value for f in x.flags})
    # fill blanks from higher-confidence values
    for field in ("project_name", "gc_name", "owner_name", "project_number", "architect_engineer"):
        cur = c.get(field) or {}
        inc = getattr(x, field)
        if (
            inc.value
            and (not cur.get("value") or inc.confidence > cur.get("confidence", 0) + 0.15)
            and field not in opp.locked_fields
        ):
            c[field] = inc.model_dump(mode="json")
    if x.kind == Kind.addendum:
        _record_addendum(session, opp, x, msg, now)
        changes.append(
            f"{x.addendum_label or 'Addendum'} received"
            + (f": {x.changes_described[:80]}" if x.changes_described else "")
        )
    if x.kind == Kind.award and x.changes_described and "cancel" in x.changes_described.lower():
        opp.status = "cancelled"
        changes.append("cancelled by sender")
    if provisional:
        opp.flags = list({*opp.flags, "possible_duplicate"})
    if changes:
        opp.changed_since_digest = True
        opp.change_summary = "; ".join(changes)
    opp.canonical = c
    opp.normalized_name = normalize_name((c.get("project_name") or {}).get("value"))
    opp.last_activity_at = now
    session.flush()


# ---------------------------------------------------------------- scoring


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a)) * 1.3  # road factor


def active_profile(session: Session) -> tuple[Profile, int]:
    row = session.scalar(select(ScoringProfile).where(ScoringProfile.active.is_(True)))
    if row is None:
        row = ScoringProfile(
            json=DEFAULT_PROFILE.model_dump(mode="json"),
            note="default",
            active=True,
            created_at=datetime.now(tz=UTC),
        )
        session.add(row)
        session.flush()
    return Profile.model_validate(row.json), row.version


def snapshot_for(
    session: Session, opp: Opportunity, *, now: datetime, home: tuple[float, float] | None
) -> tuple[OpportunitySnapshot, GCSnapshot, CalendarSnapshot]:
    c = opp.canonical
    size = c.get("size_signals") or {}
    due_raw = (c.get("bid_due") or {}).get("value")
    due = datetime.fromisoformat(due_raw) if due_raw else None
    pb_raw = (c.get("prebid") or {}).get("value")
    dist = (
        haversine_miles(home[0], home[1], opp.lat, opp.lon)
        if (home and opp.lat is not None and opp.lon is not None)
        else None
    )
    o = OpportunitySnapshot(
        opportunity_id=opp.id,
        project_type=c.get("project_type", "unknown"),
        trade_relevance=c.get("trade_relevance", "primary"),
        stated_electrical_value=size.get("stated_electrical_value"),
        stated_project_value=size.get("stated_project_value"),
        square_feet=size.get("square_feet"),
        distance_miles=dist,
        bid_due=due,
        prebid_at=datetime.fromisoformat(pb_raw) if pb_raw else None,
        prebid_mandatory=(c.get("prebid") or {}).get("mandatory"),
        bid_type=c.get("bid_type", "unknown"),
        sector=c.get("sector", "unknown"),
        flags=list(opp.flags),
        scope_items=list(c.get("scope_items") or []),
        owner_name=(c.get("owner_name") or {}).get("value"),
        status=opp.status,
        gc_is_owner_direct=False,
    )
    gc = session.get(GC, opp.gc_id) if opp.gc_id else None
    stats = session.get(GCStats, (opp.gc_id, "12m")) if opp.gc_id else None
    g = GCSnapshot(
        gc_id=gc.id if gc else None,
        name=gc.canonical_name if gc else None,
        tier=gc.tier if gc else "unknown",
        submitted_12m=stats.submitted if stats else 0,
        hit_rate_12m=stats.hit_rate if stats else None,
        key_account=gc.key_account if gc else False,
    )
    congestion = 0
    if due is not None:
        wk_start = due - timedelta(days=due.weekday())
        wk_end = wk_start + timedelta(days=7)
        for other in session.scalars(
            select(Opportunity).where(Opportunity.status == "bidding", Opportunity.id != opp.id)
        ).all():
            od = (other.canonical.get("bid_due") or {}).get("value")
            if od and wk_start <= datetime.fromisoformat(od) < wk_end:
                congestion += 1
    return o, g, CalendarSnapshot(now=now, other_bids_due_same_week=congestion)


def score_opportunity(
    session: Session,
    opp: Opportunity,
    *,
    now: datetime | None = None,
    home: tuple[float, float] | None = None,
    profile: Profile | None = None,
    profile_version: int | None = None,
) -> ScoreResult:
    now = now or datetime.now(tz=UTC)
    if profile is None:
        profile, profile_version = active_profile(session)
    o, g, cal = snapshot_for(session, opp, now=now, home=home)
    result = score(o, g, cal, profile)
    session.add(
        Score(
            opportunity_id=opp.id,
            profile_version=profile_version or 0,
            score=result.score,
            band=result.band,
            explanation=result.model_dump(mode="json"),
            inputs_hash=result.inputs_hash,
            computed_at=now,
        )
    )
    session.flush()
    return result
