"""Pipeline glue: parsed message -> stored -> extracted -> resolved -> scored.

Each step is a function over a Session so the worker, the CLI and tests share one code path.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bidtriage.core.blobs import BlobStore
from bidtriage.core.clock import Clock, SystemClock, aware
from bidtriage.core.crypto import sha256_hex
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
    OpportunityKey,
    OpportunitySource,
    Outcome,
    RawAttachment,
    RawMessage,
    Score,
    ScoringProfile,
    User,
)
from bidtriage.decisions.state import InvalidTransitionError, transition
from bidtriage.extraction.geocode import Geocoder, apply_geocode
from bidtriage.extraction.postprocess import PLATFORM_DOMAINS
from bidtriage.extraction.prefilter import obviously_not_bid
from bidtriage.extraction.prompts import PROMPT_VERSION
from bidtriage.extraction.protocol import (
    AttachmentText,
    ExtractionInput,
    Extractor,
    error_category,
)
from bidtriage.extraction.schema import (
    EXTRACTABLE_KINDS,
    KIND_REVIEW_CONFIDENCE,
    BidType,
    ExtractedOpportunity,
    Kind,
)
from bidtriage.gcs.resolve import GCRecord, resolve_gc
from bidtriage.ingestion.attachments import (
    OVERSIZE_BYTES,
    Member,
    OcrBackend,
    extract_text,
    extract_zip,
    sanitize_filename,
    sniff_mime,
)
from bidtriage.ingestion.eml import ParsedMessage
from bidtriage.ingestion.links import harvest_links
from bidtriage.resolution.evidence import (
    REVIEW,
    Candidate,
    Evidence,
    Incoming,
    decide,
    evidence,
)
from bidtriage.resolution.merge import (
    DATE_MOVERS,
    MATERIAL_SIZE_RATIO,
    addendum_gaps,
    award_outcome,
    gap_detection_enabled,
    material_change,
    merge_date,
    merge_scope,
    size_change_ratio,
    size_signals_differ,
    with_change_count,
)
from bidtriage.resolution.normalize import (
    fingerprint,
    geohash,
    name_tokens,
    normalize_domain,
    normalize_name,
)
from bidtriage.scoring.engine import ScoreResult, score
from bidtriage.scoring.profile import DEFAULT_PROFILE, Profile
from bidtriage.scoring.snapshot import CalendarSnapshot, GCSnapshot, OpportunitySnapshot

log = logging.getLogger("bidtriage.pipeline")

# ---------------------------------------------------------------- ingestion

DEDUPE_WINDOW_DAYS = 7
MAX_ATTACHMENTS = 50
"""Default cap on attachment rows per message; the caller normally passes the configured value."""


def find_duplicate(session: Session, parsed: ParsedMessage, *, now: datetime) -> RawMessage | None:
    """The two content-based duplicate rules of SPEC-01 F3 (the provider-id rule is per-source).

    Both queries take the earliest match so the link target is deterministic when several rows
    qualify (SPEC-10 F4).
    """
    if parsed.internet_message_id:
        dupe = session.scalars(
            select(RawMessage)
            .where(RawMessage.internet_message_id == parsed.internet_message_id)
            .order_by(RawMessage.received_at.asc())
            .limit(1)
        ).first()
        if dupe is not None:
            return dupe
    window = now - timedelta(days=DEDUPE_WINDOW_DAYS)
    return session.scalars(
        select(RawMessage)
        .where(RawMessage.content_hash == parsed.content_hash, RawMessage.received_at >= window)
        .order_by(RawMessage.received_at.asc())
        .limit(1)
    ).first()


def resolve_received_at(
    *,
    transport: datetime | None,
    trace: datetime | None,
    now: datetime,
    floor: datetime | None = None,
) -> datetime:
    """When we received a message (SPEC-10 F7).

    A transport-supplied time (Graph `receivedDateTime`, IMAP `INTERNALDATE`) is authoritative. The
    message's own `Received:` header is sender-controllable, so it is only a fallback for uploaded
    files and is clamped into the window ingestion is willing to believe — it feeds the 7-day dedupe
    window and the lag metric.
    """
    if transport is not None:
        return min(transport, now)
    if trace is None:
        return now
    if floor is not None and trace < floor:
        return floor
    return min(trace, now)


def refresh_copies(session: Session, message_id: str) -> int:
    """Recompute `copies` from the recipient paths on record (SPEC-10 F4).

    Derived in one place rather than incremented at each call site, so the counter cannot drift from
    `message_sources`.
    """
    count = (
        session.scalar(
            select(func.count(func.distinct(MessageSource.source_id))).where(
                MessageSource.message_id == message_id
            )
        )
        or 0
    )
    msg = session.get(RawMessage, message_id)
    if msg is not None:
        msg.copies = max(count, 1)
        return msg.copies
    return count


def ingest_parsed(
    session: Session,
    *,
    source_id: str,
    provider_message_id: str,
    parsed: ParsedMessage,
    recipient_path: str = "",
    clock: Clock | None = None,
    ocr: bool | OcrBackend | None = False,
    blobs: BlobStore | None = None,
    received_at: datetime | None = None,
    max_attachments: int | None = None,
) -> tuple[RawMessage, bool]:
    """Store a parsed message idempotently (SPEC-01 F2/F3/F5/F6). Returns (message, is_new)."""
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

    dupe = find_duplicate(session, parsed, now=now)
    if dupe is not None:
        return _link_duplicate(
            session,
            dupe,
            source_id=source_id,
            provider_message_id=provider_message_id,
            recipient_path=recipient_path,
        ), False

    forwarder = _forwarding_user(session, parsed.forwarded_by)
    msg = RawMessage(
        internet_message_id=parsed.internet_message_id,
        content_hash=parsed.content_hash,
        from_addr=parsed.from_addr,
        from_name=parsed.from_name,
        to=parsed.to,
        cc=parsed.cc,
        subject=parsed.subject,
        sent_at=parsed.sent_at,
        sent_at_confidence=parsed.sent_at_confidence,
        received_at=received_at or parsed.received_at or now,
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        body_trimmed=parsed.body_trimmed,
        headers=parsed.headers,
        in_reply_to=parsed.in_reply_to,
        references=parsed.references,
        forwarded_by_user_id=forwarder.id if forwarder else None,
        forwarded_by_addr=parsed.forwarded_by,
        forward_note=parsed.forward_note,
        forward_chain=parsed.forward_chain,
        created_at=now,
    )
    try:
        # The read above is the fast path; the partial unique index on internet_message_id is the
        # backstop when two workers race on the same message (SPEC-10 F4). A savepoint keeps the
        # rest of the transaction usable when it fires.
        with session.begin_nested():
            session.add(msg)
            session.flush()
    except IntegrityError:
        winner = find_duplicate(session, parsed, now=now)
        if winner is None:
            raise
        return _link_duplicate(
            session,
            winner,
            source_id=source_id,
            provider_message_id=provider_message_id,
            recipient_path=recipient_path,
        ), False
    session.add(
        MessageSource(
            message_id=msg.id,
            source_id=source_id,
            provider_message_id=provider_message_id,
            recipient_path=recipient_path,
        )
    )
    session.flush()
    refresh_copies(session, msg.id)
    attachment_texts = _store_attachments(
        session, msg, parsed, ocr=ocr, blobs=blobs, max_attachments=max_attachments
    )
    # SPEC-01 F6: URLs come from the body *and* the attachments (the ITB letter holds the plan-room
    # link as often as the email does).
    for link in harvest_links(parsed.body_text, parsed.body_html, *attachment_texts):
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


def _link_duplicate(
    session: Session,
    dupe: RawMessage,
    *,
    source_id: str,
    provider_message_id: str,
    recipient_path: str,
) -> RawMessage:
    """Record another path to a message we already have (SPEC-01 F3): linked, not re-created.

    One opportunity even when the GC CC'd four people, and `copies` is recomputed from the distinct
    sources so a folder re-scan handing the same mailbox a new provider id cannot inflate it.
    """
    session.add(
        MessageSource(
            message_id=dupe.id,
            source_id=source_id,
            provider_message_id=provider_message_id,
            recipient_path=recipient_path,
        )
    )
    session.flush()
    refresh_copies(session, dupe.id)
    session.flush()
    return dupe


def _forwarding_user(session: Session, address: str | None) -> User | None:
    """The forwarder becomes the default assignee suggestion downstream (SPEC-01 F4)."""
    if not address:
        return None
    return session.scalar(select(User).where(func.lower(User.email) == address.lower()))


def _store_attachments(
    session: Session,
    msg: RawMessage,
    parsed: ParsedMessage,
    *,
    ocr: bool | OcrBackend | None,
    blobs: BlobStore | None,
    max_attachments: int | None = None,
) -> list[str]:
    """Persist attachments and any container members (SPEC-01 F5). Returns their extracted texts.

    Bounded by `max_attachments` so one pathological message cannot write thousands of rows
    (SPEC-10 F2); the overflow is counted on the message for review.
    """
    cap = max_attachments if max_attachments is not None else MAX_ATTACHMENTS
    texts: list[str] = []
    stored = 0
    truncated = 0
    for a in parsed.attachments:
        if stored >= cap:
            truncated += 1
            continue
        row = _store_attachment(
            session, msg, a.filename, a.mime, a.data, parent_id=None, ocr=ocr, blobs=blobs
        )
        stored += 1
        if row.text:
            texts.append(row.text)
        for member in _members(a.filename, a.mime, a.data):
            if stored >= cap:
                truncated += 1
                continue
            child = _store_attachment(
                session,
                msg,
                member.filename,
                "application/octet-stream",
                member.data,
                parent_id=row.id,
                ocr=ocr,
                blobs=blobs,
            )
            stored += 1
            if child.text:
                texts.append(child.text)
    if truncated:
        msg.attachments_truncated = truncated
    return texts


def _members(filename: str, mime: str, data: bytes) -> list[Member]:
    if len(data) > OVERSIZE_BYTES or sniff_mime(data, mime) != "application/zip":
        return []
    return extract_zip(data).members


def _store_attachment(
    session: Session,
    msg: RawMessage,
    filename: str,
    mime: str,
    data: bytes,
    *,
    parent_id: str | None,
    ocr: bool | OcrBackend | None,
    blobs: BlobStore | None,
) -> RawAttachment:
    ext = extract_text(filename, mime, data, ocr=ocr)
    sha = sha256_hex(data)
    # SPEC-01: oversize attachments keep their metadata but their bytes are not stored.
    blob_key = None if ext.oversize or blobs is None else blobs.put(sha, data)
    row = RawAttachment(
        message_id=msg.id,
        parent_id=parent_id,
        filename=sanitize_filename(filename),
        mime=ext.mime,
        size=len(data),
        sha256=sha,
        blob_key=blob_key,
        text=ext.text,
        pages=ext.pages,
        ocr=ext.ocr,
        large_document=ext.large_document,
        oversize=ext.oversize,
        extraction_error=ext.error,
    )
    session.add(row)
    session.flush()
    return row


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
    geocoder: Geocoder | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
    force: bool = False,
) -> Extraction | None:
    """Classify and extract one message (SPEC-02).

    Returns None when the pre-filter skipped the LLM. On failure the message is left `retrying`
    until `attempt` reaches `max_attempts`, then `failed` — which is what puts it in the digest's
    Needs review list instead of losing it. The caller is expected to re-raise for the retry, so
    this records the state and lets the exception through.
    """
    clock = clock or SystemClock()
    item = build_extraction_input(session, msg, external_ref=external_ref)
    if not force and obviously_not_bid(item):
        msg.kind, msg.kind_confidence, msg.extraction_status = Kind.not_bid.value, 0.99, "skipped"
        # A message swept here after earlier failures must not keep showing the stale reason.
        msg.extraction_error = None
        session.flush()
        return None
    try:
        result = extractor.extract(item)
    except Exception as e:
        category = error_category(e)
        # Counts every try ever made, across retry rounds — `attempt` only counts this job's, and
        # a re-queued message starts a fresh job at attempt 1.
        msg.extraction_attempts += 1
        msg.extraction_error = f"{category}: {type(e).__name__}: {e}"[:400]
        msg.extraction_status = "failed" if attempt >= max_attempts else "retrying"
        session.flush()
        log.warning(
            "extraction %s message=%s attempt=%d/%d category=%s: %s",
            msg.extraction_status,
            msg.id,
            attempt,
            max_attempts,
            category,
            e,
        )
        raise
    result.location = apply_geocode(result.location, geocoder)
    return _record_extraction(session, msg, result, clock=clock)


def reextract_message(
    session: Session,
    msg: RawMessage,
    extractor: Extractor,
    *,
    external_ref: str | None = None,
    clock: Clock | None = None,
    geocoder: Geocoder | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
) -> Extraction | None:
    """Re-run extraction, keeping the previous record and pointing it at the new one (SPEC-02 F4).

    The pre-filter is bypassed: a re-extraction is either an estimator overriding the machine or a
    prompt fix being applied, and both mean "look again".
    """
    return extract_message(
        session,
        msg,
        extractor,
        external_ref=external_ref,
        clock=clock,
        geocoder=geocoder,
        attempt=attempt,
        max_attempts=max_attempts,
        force=True,
    )


def latest_extraction(session: Session, message_id: str) -> Extraction | None:
    return session.scalars(
        select(Extraction)
        .where(Extraction.message_id == message_id, Extraction.superseded_by.is_(None))
        .order_by(Extraction.version.desc(), Extraction.created_at.desc())
    ).first()


def _record_extraction(
    session: Session, msg: RawMessage, result: ExtractedOpportunity, *, clock: Clock
) -> Extraction:
    previous = latest_extraction(session, msg.id)
    msg.kind, msg.kind_confidence, msg.extraction_status = (
        result.kind.value,
        result.kind_confidence,
        "done",
    )
    msg.extraction_attempts += 1
    msg.extraction_error = None
    ext = Extraction(
        message_id=msg.id,
        version=(previous.version + 1) if previous is not None else 1,
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
    if previous is not None:
        previous.superseded_by = ext.id
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


def messages_needing_review(session: Session, *, limit: int = 100) -> list[RawMessage]:
    """Messages an estimator should look at: extraction failures and shaky classifications.

    SPEC-02 F1 and F3. A low-confidence kind is still processed as its best guess; it appears here
    so the guess can be corrected, not because anything was dropped.
    """
    return list(
        session.scalars(
            select(RawMessage)
            .where(
                or_(
                    RawMessage.extraction_status.in_(("failed", "retrying")),
                    and_(
                        RawMessage.extraction_status == "done",
                        RawMessage.kind_confidence < KIND_REVIEW_CONFIDENCE,
                    ),
                )
            )
            .order_by(RawMessage.received_at.desc())
            .limit(limit)
        ).all()
    )


RETRY_PRIORITY = 120
REEXTRACT_PRIORITY = 900


def external_ref_for(session: Session, msg: RawMessage) -> str | None:
    """Fixture stem for the offline extractor; None for anything ingested from a real mailbox."""
    link = session.scalar(select(MessageSource).where(MessageSource.message_id == msg.id))
    if link and link.provider_message_id.endswith(".eml"):
        return link.provider_message_id.rsplit(".", 1)[0]
    return None


def enqueue_extraction_retries(
    session: Session, *, clock: Clock | None = None, max_rounds: int = 24
) -> int:
    """Re-queue messages whose extraction failed, so an outage heals itself (SPEC-02 F3).

    Refusals are left alone: a policy decline is not an outage, and re-sending the same message
    hourly would only burn tokens. Re-extraction from the review page still works on them.
    """
    clock = clock or SystemClock()
    now = clock.now()
    bucket = now.strftime("%Y%m%d%H")
    queued = 0
    for msg in session.scalars(
        select(RawMessage).where(
            RawMessage.extraction_status == "failed",
            RawMessage.extraction_attempts < max_rounds,
        )
    ).all():
        if (msg.extraction_error or "").startswith("refusal"):
            continue
        if (
            enqueue(
                session,
                "extract_message",
                f"extract:{msg.id}:retry:{bucket}",
                {"message_id": msg.id},
                priority=RETRY_PRIORITY,
                clock=clock,
            )
            is not None
        ):
            queued += 1
    return queued


def enqueue_stale_prompt_reextractions(
    session: Session,
    *,
    clock: Clock | None = None,
    prompt_version: str = PROMPT_VERSION,
    window_days: int = 30,
    limit: int = 200,
) -> int:
    """Re-extract recent messages left on an older prompt version (SPEC-02 F4).

    Background and lowest priority: a prompt bump must never delay today's mail.
    """
    clock = clock or SystemClock()
    now = clock.now()
    cutoff = now - timedelta(days=window_days)
    queued = 0
    for msg in session.scalars(
        select(RawMessage)
        .where(RawMessage.extraction_status == "done", RawMessage.received_at >= cutoff)
        .order_by(RawMessage.received_at.desc())
    ).all():
        ext = latest_extraction(session, msg.id)
        if ext is None or ext.prompt_version == prompt_version:
            continue
        if (
            enqueue(
                session,
                "reextract_message",
                f"reextract:{msg.id}:{prompt_version}",
                {"message_id": msg.id},
                priority=REEXTRACT_PRIORITY,
                clock=clock,
            )
            is not None
        ):
            queued += 1
        if queued >= limit:
            break
    return queued


# ---------------------------------------------------------------- resolution

#: Roles an inbound message can take on an opportunity. `internal` is Ferry's own side of the
#: thread: kept as a source so the history is complete, but it never changes a field.
ORIGIN_ROLES = {"itb", "rfb"}

#: How wide a net the geocode pre-filter casts. Five characters is ~5 km, comfortably wider than
#: the 1 km test `evidence()` applies, so a cell boundary cannot lose a real match.
GEOHASH_PREFIX = 5

#: pg_trgm similarity floor for name candidates, applied through the `%` operator. Well below the
#: 0.85 the decision needs, so a rename still reaches `evidence()`.
#:
#: It has to be `%` and not `similarity(a, b) > x`: `gin_trgm_ops` indexes the operator, and a
#: function call in the predicate is not indexable at all — `EXPLAIN` on the function form finds no
#: index plan even with `enable_seqscan` off. `%` reads its threshold from a GUC rather than the
#: query, so the threshold is set per transaction just before the query runs.
NAME_TRIGRAM_FLOOR = 0.25

#: Ceiling on the soft-candidate set. The pre-filter is selective enough that reaching this means
#: something pathological (a GC with hundreds of live jobs); scoring stops at the most recent.
MAX_SOFT_CANDIDATES = 400


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


#: Ordered most specific first. Each entry is (namespace, pattern); the namespace keeps a Procore
#: `9912` from colliding with a BuildingConnected `9912`, because these become hard keys that merge
#: at confidence 1.0 and a collision there is a false merge (ADR-008).
#:
#: Procore's first path segment is the *company*, not the project — `app.procore.com/2318842/...`
#: is the same number for every job that GC posts — so it is deliberately skipped and the package
#: id further down the path is taken instead.
_PLATFORM_ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bc", re.compile(r"buildingconnected\.com/(?:projects|bids|rfp)/([0-9a-f]{12,})", re.I)),
    (
        "procore",
        re.compile(r"procore\.com/\d+/(?:[\w-]+/)*?(?:bid_packages|packages|projects)/(\d+)", re.I),
    ),
    ("pkg", re.compile(r"[?&]bid[_]?[Pp]ackage[_]?[Ii]d=([\w-]+)")),
    ("proj", re.compile(r"[?&](?:projectId|project_id)=([\w-]+)")),
)


def platform_ids(links: list[str]) -> set[str]:
    """Namespaced platform identifiers harvested from a message's links (SPEC-03 F2.1).

    One id per link, the most specific that matches: a bid-package id beats a project id, because
    two packages inside one project are two solicitations and only one of them is ours.
    """
    ids: set[str] = set()
    for u in links:
        for namespace, pattern in _PLATFORM_ID_PATTERNS:
            m = pattern.search(u)
            if m:
                ids.add(f"{namespace}:{m.group(1).lower()}")
                break
    return ids


def thread_ids(msg: RawMessage) -> set[str]:
    ids = (
        {str(r) for r in msg.references}
        | ({msg.in_reply_to} if msg.in_reply_to else set())
        | ({msg.internet_message_id} if msg.internet_message_id else set())
    )
    return {i for i in ids if i}


def message_links(session: Session, message_id: str) -> list[str]:
    return [
        lk.url
        for lk in session.scalars(
            select(MessageLink).where(MessageLink.message_id == message_id)
        ).all()
    ]


def _short_location(loc: dict[str, Any]) -> str | None:
    city, state = loc.get("city"), loc.get("state")
    if city and state:
        return f"{city}, {state}"
    return city or loc.get("raw")


def _is_internal(session: Session, msg: RawMessage, settings_domains: set[str]) -> bool:
    """True when this message came from Ferry's own side (SPEC-03 edge cases: internal reply).

    Own-domain configuration plus the users table, so a new estimator is recognised without a
    redeploy.
    """
    addr = (msg.from_addr or "").strip().lower()
    if not addr or "@" not in addr:
        return False
    if normalize_domain(addr) in settings_domains:
        return True
    return session.scalar(select(User.id).where(func.lower(User.email) == addr)) is not None


def _internal_domains() -> set[str]:
    from bidtriage.core.config import get_settings

    raw = get_settings().internal_domains
    return {normalize_domain(d) for d in raw.split(",") if d.strip()}


def _has_pg_trgm(session: Session) -> bool:
    """Whether trigram candidate generation is available (SPEC-03 technical notes).

    Tests and small deployments run on SQLite, and a PostgreSQL without the extension is a real
    possibility when the deploy role cannot `CREATE EXTENSION`; both fall back to token LIKE.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    cached = getattr(bind, "_bidtriage_pg_trgm", None)
    if cached is None:
        cached = bool(session.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")))
        bind._bidtriage_pg_trgm = cached  # type: ignore[union-attr]
    return bool(cached)


def candidate_ids(session: Session, i: Incoming, keys: set[tuple[str, str]]) -> list[str]:
    """Opportunity ids worth scoring against this message, cheaply (SPEC-03 F2, technical notes).

    Two passes. Hard keys resolve through `opportunity_keys` and *include archived* opportunities,
    because a reply on an archived thread should reactivate it rather than start a second copy.
    Soft candidates come from an indexed OR of GC, fingerprint, geocode bucket and name, and are
    limited to live opportunities.
    """
    hard: list[str] = []
    if keys:
        hard = list(
            session.scalars(
                select(OpportunityKey.opportunity_id).where(
                    tuple_(OpportunityKey.kind, OpportunityKey.value).in_(sorted(keys))
                )
            ).all()
        )

    clauses = []
    fp = fingerprint(i.project_name, i.gc_domain, i.city, i.bid_due)
    if any((i.project_name, i.gc_domain, i.city, i.bid_due)):
        # Skipped when there is nothing to fingerprint, or the empty-input hash would match every
        # other record we knew nothing about.
        clauses.append(Opportunity.fingerprint == fp)
    if i.gc_id:
        clauses.append(Opportunity.gc_id == i.gc_id)
    gh = geohash(i.lat, i.lon)
    if gh:
        clauses.append(Opportunity.geohash.startswith(gh[:GEOHASH_PREFIX]))
    if i.project_number:
        clauses.append(
            Opportunity.canonical["project_number"]["value"].as_string() == i.project_number
        )
    normalized = normalize_name(i.project_name)
    if normalized:
        if _has_pg_trgm(session):
            # `SET LOCAL` so the threshold is scoped to this transaction and cannot leak across a
            # pooled connection; it reverts on commit or rollback.
            session.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {NAME_TRIGRAM_FLOOR}"))
            clauses.append(Opportunity.normalized_name.op("%", is_comparison=True)(normalized))
        else:
            tokens = name_tokens(i.project_name)[:3]
            # `normalize_name` strips everything but letters, digits and spaces, so a token can
            # never carry a LIKE wildcard.
            clauses.extend(Opportunity.normalized_name.like(f"%{tok}%") for tok in tokens)
            if not tokens:
                # Every token was too short to be worth a scan ("Bldg 3" normalizes to "3"); fall
                # back to the whole normalized name so the record is still reachable.
                clauses.append(Opportunity.normalized_name == normalized)
    # No clauses means the message carries nothing to match on. An empty `or_()` is a no-op in
    # SQL, which would return every live opportunity, so stop at the hard keys instead.
    soft: list[str] = []
    if clauses:
        soft = list(
            session.scalars(
                select(Opportunity.id)
                .where(Opportunity.archived_at.is_(None), or_(*clauses))
                .order_by(Opportunity.last_activity_at.desc())
                .limit(MAX_SOFT_CANDIDATES)
            ).all()
        )
    seen: dict[str, None] = {}
    for oid in [*hard, *soft]:
        seen.setdefault(oid, None)
    return list(seen)


def load_candidates(session: Session, ids: list[str]) -> list[tuple[Opportunity, Candidate]]:
    """Build the matching inputs for a set of opportunities in a fixed number of queries."""
    if not ids:
        return []
    opps = session.scalars(select(Opportunity).where(Opportunity.id.in_(ids))).all()
    threads: dict[str, set[str]] = {}
    platforms: dict[str, set[str]] = {}
    for key in session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id.in_(ids))
    ).all():
        bucket = threads if key.kind == "thread" else platforms
        bucket.setdefault(key.opportunity_id, set()).add(key.value)
    out = []
    for o in opps:
        c = o.canonical
        due_raw = (c.get("bid_due") or {}).get("value")
        out.append(
            (
                o,
                Candidate(
                    opportunity_id=o.id,
                    project_name=(c.get("project_name") or {}).get("value"),
                    gc_id=o.gc_id,
                    gc_domain=c.get("gc_domain"),
                    city=(c.get("location") or {}).get("city"),
                    lat=o.lat,
                    lon=o.lon,
                    # `aware()` because a canonical record written before timezones were enforced
                    # (or by hand) would otherwise make the date comparison raise.
                    bid_due=aware(datetime.fromisoformat(due_raw)) if due_raw else None,
                    owner_name=(c.get("owner_name") or {}).get("value"),
                    project_number=(c.get("project_number") or {}).get("value"),
                    platform_ids=platforms.get(o.id, set()),
                    thread_ids=threads.get(o.id, set()),
                ),
            )
        )
    return out


def _remember_keys(session: Session, opportunity_id: str, keys: set[tuple[str, str]]) -> None:
    """Index a message's hard keys against the opportunity it landed on, ignoring re-adds."""
    existing = {
        (k.kind, k.value)
        for k in session.scalars(
            select(OpportunityKey).where(OpportunityKey.opportunity_id == opportunity_id)
        ).all()
    }
    for kind, value in sorted(keys - existing):
        session.add(OpportunityKey(opportunity_id=opportunity_id, kind=kind, value=value[:998]))


def resolve_message(
    session: Session,
    msg: RawMessage,
    ext: Extraction,
    *,
    clock: Clock | None = None,
    geocoder: Callable[[str], tuple[float, float] | None] | None = None,
) -> tuple[Opportunity, str]:
    """Attach a message to an opportunity (SPEC-03 F2). Returns (opportunity, decision).

    The decision is one of `merge`, `review` (attached provisionally, queued as a possible
    duplicate), `related` (a new opportunity linked to the one it resembles) or `new`.
    """
    clock = clock or SystemClock()
    now = clock.now()
    x = ExtractedOpportunity.model_validate(ext.payload)
    links = message_links(session, msg.id)
    contact_domains = [c.email for c in x.gc_contacts if c.email] + (
        [msg.from_addr]
        if msg.from_addr and not any(msg.from_addr.endswith(p) for p in PLATFORM_DOMAINS)
        else []
    )
    gc_match = resolve_gc(x.gc_name.value, contact_domains, _gc_records(session))
    # SPEC-02 F3: the directory has the last word on the GC's name. A domain match beats what the
    # letterhead said — or did not say, when the signature block was an image.
    if gc_match.gc is not None and normalize_name(x.gc_name.value or "") != normalize_name(
        gc_match.gc.canonical_name
    ):
        x.gc_name = x.gc_name.model_copy(
            update={
                "value": gc_match.gc.canonical_name,
                "confidence": max(x.gc_name.confidence, gc_match.confidence),
            }
        )
    internal = _is_internal(session, msg, _internal_domains())
    gc_id = gc_match.gc.id if gc_match.gc else None
    if gc_id is None and x.gc_name.value and not internal:
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

    # SPEC-02 F3 geocodes at extraction time; the callable is a fallback for records that
    # predate it (or tests that inject one directly).
    lat, lon = x.location.lat, x.location.lon
    if lat is None and geocoder is not None and x.location.raw:
        geo = geocoder(x.location.raw)
        if geo:
            lat, lon = geo
    keys = {("thread", t) for t in thread_ids(msg)} | {("platform", p) for p in platform_ids(links)}
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
        platform_ids=platform_ids(links),
        thread_ids=thread_ids(msg),
        kind=x.kind.value,
    )

    scored = [
        (o, evidence(cand, incoming))
        for o, cand in load_candidates(session, candidate_ids(session, incoming, keys))
    ]
    best: tuple[float, Opportunity | None, Evidence | None] = (-1.0, None, None)
    if scored:
        o, ev = min(scored, key=lambda row: _match_rank(row[0], row[1], incoming))
        best = (ev.score, o, ev)

    decision = "new"
    if best[1] is not None and best[2] is not None:
        decision = decide(best[2], incoming_kind=x.kind.value)

    if decision in ("merge", "review") and best[1] is not None and best[2] is not None:
        opp = best[1]
        if opp.archived_at is not None:
            _reactivate(opp, now)
        role = "internal" if internal else x.kind.value
        if internal:
            # Ferry's own reply-all belongs in the history, but nothing an estimator wrote to a GC
            # is an authoritative statement of the GC's dates or scope (SPEC-03 edge cases).
            opp.last_activity_at = now
        else:
            _apply_update(session, opp, x, msg, now, provisional=(decision == "review"))
        if best[2].hard_key is not None:
            # A platform project id or a shared thread settles the identity, so an earlier
            # provisional attach is no longer an open question for a human.
            opp.flags = [f for f in opp.flags if f != "possible_duplicate"]
        _attach_source(session, opp, msg, role, now, best[2], provisional=decision == "review")
        _remember_keys(session, opp.id, keys)
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
            geohash=geohash(lat, lon),
            first_seen_at=now,
            last_activity_at=now,
            flags=[f.value for f in x.flags],
        )
        if x.kind.value not in ORIGIN_ROLES:
            # SPEC-03 F2.4: an update that matches nothing becomes a visible stub rather than a
            # dropped message — the missing original is the thing worth seeing.
            opp.flags = [*opp.flags, "orphan_update"]
        session.add(opp)
        session.flush()
        _attach_source(
            session,
            opp,
            msg,
            "internal" if internal else "origin",
            now,
            best[2] if decision == "related" else None,
            provisional=False,
        )
        _remember_keys(session, opp.id, keys)
        if decision == "related" and best[1] is not None:
            _link_related(opp, best[1], best[2])
        if x.kind == Kind.addendum or x.addendum_label or x.addendum_number:
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


def _match_rank(opp: Opportunity, ev: Evidence, incoming: Incoming) -> tuple[float, float, float]:
    """Sort key for picking the best candidate: highest score, then nearest date, then most recent.

    Two rebids of one project score identically on name, GC and geography, so without a tiebreak an
    addendum would attach to whichever row the database happened to return first. The date the
    message itself states is the honest way to choose between them.
    """
    candidate_due = (opp.canonical.get("bid_due") or {}).get("value")
    if candidate_due and incoming.bid_due:
        distance = abs(
            (
                aware(datetime.fromisoformat(candidate_due)) - incoming.bid_due  # type: ignore[operator]
            ).total_seconds()
        )
    else:
        distance = float("inf")
    last = aware(opp.last_activity_at)
    return (-ev.score, distance, -(last.timestamp() if last else 0.0))


def possible_duplicates(
    session: Session, opp: Opportunity, *, limit: int = 10
) -> list[tuple[Opportunity, Evidence]]:
    """Opportunities this one resembles, best first — the review page's merge picker (SPEC-03 F2.3).

    Same candidate generation and same scoring as automatic resolution, so what a human sees is
    what the matcher saw.
    """
    c = opp.canonical
    due_raw = (c.get("bid_due") or {}).get("value")
    mine = Incoming(
        project_name=(c.get("project_name") or {}).get("value"),
        gc_id=opp.gc_id,
        gc_domain=c.get("gc_domain"),
        city=(c.get("location") or {}).get("city"),
        lat=opp.lat,
        lon=opp.lon,
        bid_due=aware(datetime.fromisoformat(due_raw)) if due_raw else None,
        owner_name=(c.get("owner_name") or {}).get("value"),
        project_number=(c.get("project_number") or {}).get("value"),
        kind=c.get("kind", "itb"),
    )
    keys = {
        (k.kind, k.value)
        for k in session.scalars(
            select(OpportunityKey).where(OpportunityKey.opportunity_id == opp.id)
        ).all()
    }
    scored = [
        (other, evidence(cand, mine))
        for other, cand in load_candidates(session, candidate_ids(session, mine, keys))
        if other.id != opp.id
    ]
    # Only the review band. Below 0.6 the matcher already decided these are different projects, and
    # offering them as merge targets would invite the false merge ADR-008 is built to avoid.
    in_band = [row for row in scored if row[1].score >= REVIEW]
    return sorted(in_band, key=lambda row: row[1].score, reverse=True)[:limit]


def _attach_source(
    session: Session,
    opp: Opportunity,
    msg: RawMessage,
    role: str,
    now: datetime,
    ev: Evidence | None,
    *,
    provisional: bool,
) -> None:
    if session.get(OpportunitySource, (opp.id, msg.id)) is not None:
        return
    session.add(
        OpportunitySource(
            opportunity_id=opp.id,
            message_id=msg.id,
            role=role,
            attached_at=now,
            evidence={
                "score": ev.score,
                "hard_key": ev.hard_key,
                "components": ev.components,
                "notes": ev.notes,
                "provisional": provisional,
            }
            if ev is not None
            else {},
        )
    )


def _link_related(new: Opportunity, other: Opportunity, ev: Evidence | None) -> None:
    """Two solicitations for one real-world project, kept apart but visible to each other."""
    new.related_project_ids = sorted({*new.related_project_ids, other.id})
    other.related_project_ids = sorted({*other.related_project_ids, new.id})
    if ev is not None and ev.rebid:
        new.flags = sorted({*new.flags, "rebid_of_related"})


def _reactivate(opp: Opportunity, now: datetime) -> None:
    """A message on an archived opportunity's thread brings it back, flagged (SPEC-03 edge cases)."""
    opp.archived_at = None
    opp.flags = sorted({*opp.flags, "reactivated"})
    if opp.status == "archived":
        opp.status = "undecided"
    opp.changed_since_digest = True
    opp.last_activity_at = now


def _canonical_from(
    x: ExtractedOpportunity, gc_domain: str | None, as_of: datetime | None = None
) -> dict[str, Any]:
    c = x.model_dump(mode="json")
    c["gc_domain"] = gc_domain
    c["delivery_channels"] = [x.delivery_channel.value]
    if as_of is not None:
        for field in ("bid_due", "rfi_deadline", "intent_due"):
            if (c.get(field) or {}).get("value"):
                c[field]["as_of"] = as_of.isoformat()
    return c


def _record_addendum(
    session: Session, opp: Opportunity, x: ExtractedOpportunity, msg: RawMessage, now: datetime
) -> str | None:
    """Add or count an addendum. Returns a change note, or None when this is a duplicate copy.

    Numbering gaps raise `addendum_gap`, unless the opportunity carries a label we cannot order
    ("Addendum A", "Bulletin 1"), in which case the check is off for that opportunity.
    """
    label = x.addendum_label or (
        f"Addendum {x.addendum_number}" if x.addendum_number else f"Addendum ({msg.subject[:30]})"
    )
    label = label[:50]
    existing = session.scalar(
        select(Addendum).where(Addendum.opportunity_id == opp.id, Addendum.label == label)
    )
    note: str | None
    if existing is None:
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
        note = f"{label} received" + (
            f": {x.changes_described[:80]}" if x.changes_described else ""
        )
        session.add(
            FieldHistory(
                opportunity_id=opp.id,
                field="addenda",
                old={"labels": _addendum_labels(session, opp.id, exclude=label)},
                new={"label": label, "number": x.addendum_number},
                message_id=msg.id,
                applied=True,
                changed_at=now,
            )
        )
    elif existing.message_id != msg.id:
        # CC fan-out, or the same addendum forwarded again: one record, a higher copy count.
        existing.copies += 1
        note = None
    else:
        note = None
    rows = session.scalars(select(Addendum).where(Addendum.opportunity_id == opp.id)).all()
    gaps = (
        addendum_gaps([a.number for a in rows if a.number is not None])
        if gap_detection_enabled([a.label for a in rows])
        else []
    )
    if gaps:
        opp.flags = sorted({*opp.flags, "addendum_gap"})
    else:
        opp.flags = [f for f in opp.flags if f != "addendum_gap"]
    return note


def _addendum_labels(
    session: Session, opportunity_id: str, *, exclude: str | None = None
) -> list[str]:
    return sorted(
        a.label
        for a in session.scalars(
            select(Addendum).where(Addendum.opportunity_id == opportunity_id)
        ).all()
        if a.label != exclude
    )


def set_status(
    session: Session,
    opp: Opportunity,
    target: str,
    *,
    at: datetime,
    message_id: str | None = None,
    user_id: str | None = None,
) -> bool:
    """Move an opportunity's status and record it in `field_history` (SPEC-03 F4, F5).

    Returns False when the lifecycle forbids the move, so an award email for a job already marked
    won does not raise out of the middle of resolution.
    """
    previous = opp.status
    if previous == target:
        return False
    try:
        opp.status = transition(previous, target)
    except InvalidTransitionError:
        # The lifecycle says no, but a sender telling us the job is over is not something to
        # drop on the floor. Record it unapplied so it reaches the history and the digest, the
        # same way a locked field does (SPEC-03 F3, F4).
        log.info("refused status %s → %s for %s", previous, target, opp.id)
        session.add(
            FieldHistory(
                opportunity_id=opp.id,
                field="status",
                old={"value": previous},
                new={"value": target},
                message_id=message_id,
                user_id=user_id,
                applied=False,
                changed_at=at,
            )
        )
        return False
    session.add(
        FieldHistory(
            opportunity_id=opp.id,
            field="status",
            old={"value": previous},
            new={"value": target},
            message_id=message_id,
            user_id=user_id,
            applied=True,
            changed_at=at,
        )
    )
    if target == "archived":
        opp.archived_at = at
    return True


def _apply_update(
    session: Session,
    opp: Opportunity,
    x: ExtractedOpportunity,
    msg: RawMessage,
    now: datetime,
    *,
    provisional: bool,
) -> None:
    """Merge one message into an opportunity's canonical fields (SPEC-03 F3, F4).

    Every accepted change and every rejected one writes `field_history`; a rejected change
    (`applied=False`) is how a locked field or a stale message stays visible instead of silent.
    """
    c = dict(opp.canonical)
    changes: list[str] = []
    material = False

    def record(field: str, old: Any, new: Any, *, applied: bool) -> None:
        session.add(
            FieldHistory(
                opportunity_id=opp.id,
                field=field,
                old=old,
                new=new,
                message_id=msg.id,
                applied=applied,
                changed_at=now,
            )
        )

    for field in ("bid_due", "rfi_deadline", "intent_due"):
        # Counted before anything is recorded, and only for the field whose summary shows it.
        prior_moves = _applied_change_count(session, opp.id, field) if field == "bid_due" else 0
        existing_raw = (c.get(field) or {}).get("value")
        # `aware()` for the same reason `load_candidates` uses it: a canonical record written by
        # hand, or before timezones were enforced, would otherwise make the comparison below raise.
        existing = aware(datetime.fromisoformat(existing_raw)) if existing_raw else None
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
            and x.kind.value in DATE_MOVERS
        ):
            # An older message arriving late must not move a newer date backwards; it is still
            # recorded, unapplied, so the disagreement is visible.
            record(
                field,
                {"value": existing_raw},
                {"value": incoming.isoformat()},
                applied=False,
            )
            continue
        out = merge_date(
            existing,
            incoming,
            incoming_kind=x.kind.value,
            locked=field in opp.locked_fields,
            field=field,
        )
        if out.changed or out.conflict:
            record(
                field,
                {"value": existing_raw},
                {"value": incoming.isoformat() if incoming else None},
                applied=out.changed,
            )
        if out.changed:
            c[field] = {
                **getattr(x, field).model_dump(mode="json"),
                "as_of": (aware(msg.sent_at) or now).isoformat(),
            }
            note = out.note + (f" ({x.addendum_label})" if x.addendum_label else "")
            if field == "bid_due":
                note = with_change_count(note, prior_moves + 1)
                material = material or material_change(old_due=existing, new_due=incoming)
                _break_snooze_if_due_sooner(session, opp, incoming, now, changes)
            changes.append(note)
        if out.conflict:
            # `date_conflict` means two sources disagree (SPEC-03 F3, the reminder rule).
            # A value the estimator locked is a different thing and gets its own flag.
            locked = field in opp.locked_fields
            opp.flags = sorted({*opp.flags, "locked_conflict" if locked else "date_conflict"})
            changes.append(out.note)

    # Pre-bid carries a location and a mandatory flag alongside its datetime, so it merges on its own.
    existing_pb = (c.get("prebid") or {}).get("value")
    if (
        x.prebid.value
        and (not existing_pb or aware(datetime.fromisoformat(existing_pb)) != x.prebid.value)
        and x.kind.value in DATE_MOVERS
    ):
        if "prebid" in opp.locked_fields:
            record(
                "prebid",
                {"value": existing_pb},
                {"value": x.prebid.value.isoformat()},
                applied=False,
            )
            opp.flags = sorted({*opp.flags, "locked_conflict"})
        else:
            record(
                "prebid",
                {"value": existing_pb},
                {"value": x.prebid.value.isoformat()},
                applied=True,
            )
            c["prebid"] = x.prebid.model_dump(mode="json")
            changes.append(
                f"pre-bid {'set' if not existing_pb else 'moved'} to {x.prebid.value:%b %d %I:%M %p}"
            )

    # Scope is a union across sources; only an addendum that says so in words takes anything out.
    scope = merge_scope(
        list(c.get("scope_items") or []),
        [s.value for s in x.scope_items],
        changes_described=x.changes_described if x.kind == Kind.addendum else None,
    )
    if scope.value != sorted(c.get("scope_items") or []):
        record("scope_items", {"value": c.get("scope_items")}, {"value": scope.value}, applied=True)
        c["scope_items"] = scope.value
        if scope.added:
            changes.append("scope adds " + ", ".join(a.replace("_", " ") for a in scope.added))
        if scope.removed:
            opp.flags = sorted({*opp.flags, "removed_by_addendum"})
            changes.append(
                f"{x.addendum_label or 'addendum'} removes "
                + ", ".join(r.replace("_", " ") for r in scope.removed)
            )

    # Which channels this solicitation has reached us through. The invite and the GC's own email
    # are one opportunity, and the digest should still say it arrived both ways (SPEC-03 F1).
    channels = sorted({*(c.get("delivery_channels") or []), x.delivery_channel.value})
    if channels != sorted(c.get("delivery_channels") or []):
        c["delivery_channels"] = channels
        if len(channels) > 1:
            changes.append("also arrived via " + x.delivery_channel.value)

    new_flags = sorted(set(opp.flags) | {f.value for f in x.flags})
    if new_flags != sorted(opp.flags):
        record("flags", {"value": sorted(opp.flags)}, {"value": new_flags}, applied=True)
    opp.flags = new_flags

    if x.bid_type != BidType.unknown and x.bid_type.value != c.get("bid_type"):
        if "bid_type" in opp.locked_fields:
            record(
                "bid_type", {"value": c.get("bid_type")}, {"value": x.bid_type.value}, applied=False
            )
            opp.flags = sorted({*opp.flags, "locked_conflict"})
        else:
            record(
                "bid_type", {"value": c.get("bid_type")}, {"value": x.bid_type.value}, applied=True
            )
            changes.append(f"bid type {c.get('bid_type', 'unknown')} → {x.bid_type.value}")
            c["bid_type"] = x.bid_type.value

    incoming_size = x.size_signals.model_dump(mode="json")
    if size_signals_differ(c.get("size_signals"), incoming_size):
        if "size_signals" in opp.locked_fields:
            record(
                "size_signals",
                {"value": c.get("size_signals")},
                {"value": incoming_size},
                applied=False,
            )
            opp.flags = sorted({*opp.flags, "locked_conflict"})
        else:
            ratio = size_change_ratio(c.get("size_signals"), incoming_size)
            record(
                "size_signals",
                {"value": c.get("size_signals")},
                {"value": incoming_size},
                applied=True,
            )
            merged_size = {**(c.get("size_signals") or {})}
            merged_size.update({k: v for k, v in incoming_size.items() if v is not None})
            c["size_signals"] = merged_size
            material = material or ratio > MATERIAL_SIZE_RATIO
            changes.append(f"size restated ({ratio:.0%} change)" if ratio else "size details added")

    # Fill blanks, and let a clearly better-sourced value win; never overwrite a locked field.
    for field in ("project_name", "gc_name", "owner_name", "project_number", "architect_engineer"):
        cur = c.get(field) or {}
        inc = getattr(x, field)
        if not inc.value or inc.value == cur.get("value"):
            continue
        better = not cur.get("value") or inc.confidence > cur.get("confidence", 0) + 0.15
        if field in opp.locked_fields:
            if better:
                # SPEC-03 F3: "system saw a different value" — recorded, not applied.
                record(field, {"value": cur.get("value")}, {"value": inc.value}, applied=False)
                opp.flags = sorted({*opp.flags, "locked_conflict"})
            continue
        if better:
            c[field] = inc.model_dump(mode="json")

    if x.kind == Kind.addendum or x.addendum_label or x.addendum_number:
        # Either an addendum in its own right, or a date change that also carries one — a message
        # can have both effects (SPEC-03 edge cases). An addendum that names no number at all
        # still gets a record, under a label derived from its subject.
        addendum_note = _record_addendum(session, opp, x, msg, now)
        if addendum_note:
            changes.append(addendum_note)

    if x.kind == Kind.award:
        outcome = award_outcome(x.changes_described, x.summary, msg.subject)
        if outcome and outcome != opp.status:
            if set_status(session, opp, outcome, at=now, message_id=msg.id):
                # SPEC-03 edge cases: the outcome is recorded with the message it came from, so an
                # estimator correcting it can see what the machine read and where.
                session.add(
                    Outcome(
                        opportunity_id=opp.id,
                        result=outcome,
                        notes=(x.changes_described or x.summary or msg.subject)[:500],
                        source_message_id=msg.id,
                        created_at=now,
                    )
                )
                changes.append(
                    "cancelled by sender"
                    if outcome == "cancelled"
                    else f"marked {outcome} by sender"
                )
            else:
                # Refused by the lifecycle. Say so out loud rather than silently ignoring an
                # award notice, which is the one message that closes a job out.
                opp.flags = sorted({*opp.flags, "outcome_conflict"})
                changes.append(f"sender says {outcome}, but this is {opp.status}; not applied")

    if provisional:
        opp.flags = sorted({*opp.flags, "possible_duplicate"})
    if changes:
        opp.changed_since_digest = True
        opp.material_change = opp.material_change or material
        opp.change_summary = "; ".join(changes)
    opp.canonical = c
    opp.normalized_name = normalize_name((c.get("project_name") or {}).get("value"))
    opp.last_activity_at = now
    session.flush()


def _applied_change_count(session: Session, opportunity_id: str, field: str) -> int:
    """How many times this field has already moved."""
    prior = session.scalar(
        select(func.count())
        .select_from(FieldHistory)
        .where(
            FieldHistory.opportunity_id == opportunity_id,
            FieldHistory.field == field,
            FieldHistory.applied.is_(True),
        )
    )
    return int(prior or 0)


def _break_snooze_if_due_sooner(
    session: Session,
    opp: Opportunity,
    new_due: datetime | None,
    now: datetime,
    changes: list[str],
) -> None:
    """A date that moves in front of a snooze ends the snooze (SPEC-03 edge cases).

    Sleeping through a bid date is the failure this whole system exists to prevent, so the snooze
    loses and the opportunity goes back on the decision list, flagged so the override is visible.
    """
    if opp.status != "snoozed" or new_due is None:
        return
    until = aware(opp.snooze_until)
    if until is None or new_due > until:
        return
    if set_status(session, opp, "undecided", at=now):
        opp.snooze_until = None
        opp.flags = sorted({*opp.flags, "snooze_overridden"})
        changes.append("snooze ended early: bid date now falls before the snooze")


def archive_stale(session: Session, *, now: datetime, after_days: int = 180) -> int:
    """Archive opportunities untouched for 180 days that were never bid (SPEC-03 F5)."""
    cutoff = now - timedelta(days=after_days)
    archived = 0
    for opp in session.scalars(
        select(Opportunity).where(
            Opportunity.archived_at.is_(None),
            Opportunity.status.in_(["new", "undecided", "passed", "snoozed", "cancelled"]),
            Opportunity.last_activity_at < cutoff,
        )
    ).all():
        if set_status(session, opp, "archived", at=now):
            archived += 1
    return archived


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
