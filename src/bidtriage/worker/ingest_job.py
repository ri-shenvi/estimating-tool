"""Polling orchestration for SPEC-01 F7/F8 and SPEC-10 F1/F2/F5/F6/F8/F10.

`run_poll` drives a source's `FetchSession`: it stores one message at a time, reports each outcome
back to the session, and only then persists the cursor the session computes. That is what makes a
transient fetch failure a retry rather than a silent loss, and what keeps memory bounded by
`INGEST_MAX_BATCH_BYTES` instead of by the size of the folder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from bidtriage.core.blobs import BlobStore
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import (
    RawAttachment,
    RawMessage,
    Source,
    SourcePoll,
    SourceSkip,
    User,
)
from bidtriage.ingestion.health import DOWN, SourceHealth, source_health
from bidtriage.ingestion.protocol import (
    FetchOptions,
    FetchSession,
    PollSummary,
    supports_backfill,
)
from bidtriage.worker import pipeline

if TYPE_CHECKING:  # pragma: no cover
    from bidtriage.worker.handlers import Context

log = logging.getLogger("bidtriage.ingest")

BACKFILL_PRIORITY = 80
"""Jobs are claimed in ascending priority, so 80 always yields to a due poll_source at 50 and a
backfill can never starve live mail (SPEC-01 F7)."""

ABANDONED_POLL_AFTER = timedelta(minutes=30)


class Mailer(Protocol):
    def __call__(self, *, to: str, subject: str, html: str, text: str) -> str: ...


# ---------------------------------------------------------------------- polling


def run_poll(
    session: Session,
    src: Source,
    impl: Any,
    ctx: Context,
    *,
    mode: str = "live",
    limit: int | None = None,
) -> PollSummary:
    """Fetch and store one batch from `src`. Writes a `source_poll` row either way (SPEC-01 F7)."""
    poll = SourcePoll(
        source_id=src.id, mode=mode, started_at=datetime.now(tz=UTC), errors=[], error_count=0
    )
    session.add(poll)
    session.commit()  # the poll row must survive a crash inside the batch

    summary = PollSummary()
    try:
        if mode == "live":
            _seed_live_cursor(session, src, impl)
        options = _options(session, src, ctx, mode=mode, limit=limit)
        fetch_session = impl.fetch(dict(src.delta_state), options)
        summary = _drain(session, src, fetch_session, ctx)
        src.delta_state = fetch_session.new_state()
    except Exception as e:
        # The mailbox could not be reached at all: this is what makes a source degrade and then go
        # down (SPEC-01 F8), so `last_success_at` is deliberately left alone.
        summary.errors.append(f"poll failed: {e}")
        _finish(session, poll, summary)
        src.status = "error"
        session.commit()
        raise

    # We reached the mailbox, so the poll succeeded even if individual messages were unreadable;
    # otherwise one poison message would eventually report a healthy mailbox as down. Messages that
    # failed stay in front of the cursor, and `source_skips` records any we have given up on.
    src.last_success_at = datetime.now(tz=UTC)
    src.status = "warning" if summary.errors else "active"
    _finish(session, poll, summary)
    session.commit()
    return summary


def _options(
    session: Session, src: Source, ctx: Context, *, mode: str, limit: int | None
) -> FetchOptions:
    settings = ctx.settings
    return FetchOptions(
        limit=limit
        or (settings.ingest_backfill_batch if mode == "backfill" else settings.ingest_live_batch),
        max_bytes=settings.ingest_max_batch_bytes,
        since=(datetime.now(tz=UTC) - timedelta(days=src.backfill_days))
        if mode == "backfill"
        else None,
        skip_ids=given_up_ids(session, src.id),
    )


def given_up_ids(session: Session, source_id: str) -> frozenset[str]:
    """Provider ids ingestion has stopped retrying, so a cursor may move past them (SPEC-10 F1)."""
    return frozenset(
        session.scalars(
            select(SourceSkip.provider_message_id).where(
                SourceSkip.source_id == source_id, SourceSkip.given_up.is_(True)
            )
        ).all()
    )


def _drain(session: Session, src: Source, fetch_session: FetchSession, ctx: Context) -> PollSummary:
    """Store each fetched message on its own transaction and report the outcome to the session."""
    summary = PollSummary()
    blobs = _blobs(ctx)
    settings = ctx.settings
    floor = datetime.now(tz=UTC) - timedelta(days=max(src.backfill_days, 1))
    for item in fetch_session:
        summary.seen += 1
        received_at = pipeline.resolve_received_at(
            transport=item.received_at,
            trace=item.parsed.received_at,
            now=datetime.now(tz=UTC),
            floor=floor,
        )
        try:
            _, is_new = pipeline.ingest_parsed(
                session,
                source_id=src.id,
                provider_message_id=item.provider_message_id,
                parsed=item.parsed,
                recipient_path=src.mailbox or src.name,
                ocr=settings.ingest_ocr,
                blobs=blobs,
                received_at=received_at,
                max_attachments=settings.ingest_max_attachments_per_message,
            )
            session.commit()
        except Exception as e:  # noqa: BLE001 - one poison message must not block the mailbox
            session.rollback()
            fetch_session.record(item.provider_message_id, stored=False, error=str(e))
            log.exception(
                "ingest failed source=%s provider_id=%s", src.id, item.provider_message_id
            )
            continue
        fetch_session.record(item.provider_message_id, stored=True)
        summary.new += int(is_new)
        summary.duplicates += int(not is_new)

    summary.more_available = fetch_session.more_available
    summary.errors = fetch_session.all_errors
    summary.skipped = len(fetch_session.passed_over)
    _record_outcomes(session, src, fetch_session, ctx)
    return summary


def _record_outcomes(
    session: Session, src: Source, fetch_session: FetchSession, ctx: Context
) -> None:
    """Maintain `source_skips`: count failures, clear on success, give up past the limit (F1)."""
    now = datetime.now(tz=UTC)
    limit = ctx.settings.ingest_max_fetch_attempts
    for provider_id in fetch_session.stored:
        session.execute(
            delete(SourceSkip).where(
                SourceSkip.source_id == src.id,
                SourceSkip.provider_message_id == provider_id,
            )
        )
    for provider_id, error in fetch_session.failures.items():
        row = session.get(SourceSkip, {"source_id": src.id, "provider_message_id": provider_id})
        if row is None:
            row = SourceSkip(
                source_id=src.id,
                provider_message_id=provider_id,
                first_seen_at=now,
                last_attempt_at=now,
                attempts=1,
                last_error=error[:2000],
                given_up=False,
            )
            session.add(row)
        else:
            row.attempts += 1
            row.last_attempt_at = now
            row.last_error = error[:2000]
        if row.attempts >= limit and not row.given_up:
            # Out of retries: let the cursor past it on the next poll, and surface it for review
            # rather than dropping it silently.
            row.given_up = True
            log.error(
                "giving up on message source=%s provider_id=%s after %d attempts: %s",
                src.id,
                provider_id,
                row.attempts,
                error,
            )
    session.flush()


def _seed_live_cursor(session: Session, src: Source, impl: Any) -> None:
    """Point a brand-new source's live cursor at *now* so history is the backfill's job (F7).

    Without this the first delta query or UID scan would drain the whole folder, ignoring the
    configured backfill window. Requires both `seed` and backfill support: parking the cursor on a
    source that cannot walk history would skip that history entirely (SPEC-10 F5).
    """
    if src.delta_state or src.backfill_days <= 0 or not supports_backfill(impl):
        return
    src.delta_state = impl.seed({})
    session.commit()


def _blobs(ctx: Context) -> BlobStore | None:
    blobs = getattr(ctx, "blobs", None)
    if blobs is not None:
        return blobs
    from bidtriage.core.blobs import get_blob_store

    return get_blob_store()


def _finish(session: Session, poll: SourcePoll, summary: PollSummary) -> None:
    poll.finished_at = datetime.now(tz=UTC)
    poll.seen, poll.new, poll.duplicates = summary.seen, summary.new, summary.duplicates
    poll.skipped = summary.skipped
    poll.errors = list(summary.errors)
    poll.error_count = len(summary.errors)
    session.flush()


# ---------------------------------------------------------------------- backfill (F5)


def run_backfill(session: Session, src: Source, impl: Any, ctx: Context) -> PollSummary:
    """One backfill batch, oldest-first. Chains the next batch behind live polls (SPEC-01 F7).

    Failures are counted on the source so a backfill that cannot progress reports itself rather than
    stalling silently (SPEC-10 F5).
    """
    if src.backfill_done or src.backfill_stuck:
        return PollSummary()
    if not supports_backfill(impl):
        src.backfill_done = True
        session.commit()
        return PollSummary()
    try:
        summary = run_poll(
            session, src, impl, ctx, mode="backfill", limit=ctx.settings.ingest_backfill_batch
        )
    except Exception as e:
        _note_backfill_failure(session, src, ctx, str(e))
        raise
    if summary.errors:
        _note_backfill_failure(session, src, ctx, "; ".join(summary.errors[:3]))
    else:
        src.backfill_attempts = 0
        src.backfill_last_error = None
    if summary.more_available:
        batch = int(src.delta_state.get("backfill:batch", 0)) + 1
        src.delta_state = {**src.delta_state, "backfill:batch": batch}
        enqueue(
            session,
            "backfill_source",
            f"backfill:{src.id}:{batch}",
            {"source_id": src.id},
            priority=BACKFILL_PRIORITY,
        )
    elif not summary.errors:
        src.backfill_done = True
    session.commit()
    return summary


def _note_backfill_failure(session: Session, src: Source, ctx: Context, error: str) -> None:
    src.backfill_attempts += 1
    src.backfill_last_error = error[:2000]
    if src.backfill_attempts >= ctx.settings.ingest_max_backfill_attempts:
        src.backfill_stuck = True
        log.error(
            "backfill stuck for source=%s after %d attempts: %s",
            src.id,
            src.backfill_attempts,
            error,
        )
    session.commit()


# ---------------------------------------------------------------------- maintenance (F6)


def close_abandoned_polls(
    session: Session, *, now: datetime | None = None, older_than: timedelta | None = None
) -> int:
    """Close poll rows left open by a worker that died mid-batch (SPEC-10 F6).

    Otherwise the dangling row stays the source's apparent last poll forever.
    """
    now = now or datetime.now(tz=UTC)
    cutoff = now - (older_than or ABANDONED_POLL_AFTER)
    from bidtriage.core.clock import aware

    closed = 0
    for poll in session.scalars(select(SourcePoll).where(SourcePoll.finished_at.is_(None))).all():
        started = aware(poll.started_at)
        if started is None or started > cutoff:
            continue
        poll.finished_at = now
        poll.errors = [*list(poll.errors), "abandoned: worker died mid-poll"]
        poll.error_count = len(poll.errors)
        closed += 1
    session.flush()
    return closed


def prune_polls(session: Session, *, now: datetime | None = None, retention_days: int = 30) -> int:
    """Bound `source_polls`: one poll per 5 minutes per source is ~8.6k rows a month (SPEC-10 F6)."""
    now = now or datetime.now(tz=UTC)
    cutoff = now - timedelta(days=retention_days)
    result = session.execute(
        delete(SourcePoll).where(
            SourcePoll.started_at < cutoff, SourcePoll.finished_at.is_not(None)
        )
    )
    session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


def latest_poll(session: Session, source_id: str) -> SourcePoll | None:
    """The most recent poll for a source, with an explicit LIMIT (SPEC-10 F6)."""
    return session.scalars(
        select(SourcePoll)
        .where(SourcePoll.source_id == source_id)
        .order_by(SourcePoll.started_at.desc())
        .limit(1)
    ).first()


# ---------------------------------------------------------------------- health (F8, F10)


def check_sources(
    session: Session,
    ctx: Context,
    *,
    now: datetime | None = None,
    mailer: Mailer | None = None,
) -> list[tuple[Source, SourceHealth]]:
    """Refresh every source's health and alert once per outage, not once per poll (SPEC-01 F8)."""
    now = now or datetime.now(tz=UTC)
    from bidtriage.core.clock import aware

    out: list[tuple[Source, SourceHealth]] = []
    for src in session.scalars(select(Source)).all():
        health = source_health(
            last_success_at=aware(src.last_success_at), now=now, paused=src.paused
        )
        out.append((src, health))
        if health.status == DOWN and src.down_alert_sent_at is None:
            if _alert(session, ctx, src, health, mailer=mailer):
                src.down_alert_sent_at = now
        elif health.status != DOWN and src.down_alert_sent_at is not None:
            src.down_alert_sent_at = None  # re-arm the alert for the next outage
    session.flush()
    return out


def _alert(
    session: Session, ctx: Context, src: Source, health: SourceHealth, *, mailer: Mailer | None
) -> bool:
    """Notify the admins. True if at least one recipient was reached (SPEC-10 F10).

    All-or-nothing would mean one bad address re-sent the whole alert on every sweep.
    """
    recipients = _alert_recipients(session, ctx)
    if not recipients:
        log.error("source down but no admin alert recipient configured: %s", src.name)
        return False
    subject = f"[bidtriage] mail source down: {src.name}"
    text = (
        f"{src.name} ({src.kind}) has not polled successfully: {health.detail}.\n\n"
        f"Ingestion is stopped for this mailbox. See {ctx.settings.base_url}/admin/ and "
        "docs/runbooks/connect-m365-mailbox.md."
    )
    send = mailer or _smtp_mailer(ctx)
    delivered = 0
    for to in recipients:
        try:
            send(to=to, subject=subject, html=f"<p>{text}</p>", text=text)
            delivered += 1
        except Exception:  # noqa: BLE001 - a broken SMTP must not fail the health job
            log.exception("could not send source-down alert to %s", to)
    return delivered > 0


def _alert_recipients(session: Session, ctx: Context) -> list[str]:
    if ctx.settings.admin_alert_to:
        return [a.strip() for a in ctx.settings.admin_alert_to.split(",") if a.strip()]
    return [
        u.email
        for u in session.scalars(
            select(User).where(User.role.in_(["admin", "chief"]), User.active.is_(True))
        ).all()
    ]


def _smtp_mailer(ctx: Context) -> Mailer:
    from bidtriage.digest.sender import send_email

    def send(*, to: str, subject: str, html: str, text: str) -> str:
        return send_email(
            smtp_url=ctx.settings.smtp_url,
            sender=ctx.settings.digest_from,
            to=to,
            subject=subject,
            html=html,
            text=text,
        )

    return send


# ---------------------------------------------------------------------- metrics (F8)


@dataclass
class IngestionMetrics:
    """The SPEC-01 metrics, over a trailing window. Computed with aggregates only (SPEC-10 F8)."""

    window_hours: int
    polls: int = 0
    polls_failed: int = 0
    messages_new: int = 0
    duplicates_linked: int = 0
    messages_skipped: int = 0
    attachments_extracted: int = 0
    attachments_failed: int = 0
    lag_p50_seconds: float | None = None
    lag_p95_seconds: float | None = None
    clock_skew: int = 0

    @property
    def poll_success_rate(self) -> float | None:
        return None if not self.polls else (self.polls - self.polls_failed) / self.polls


LAG_SAMPLE = 5000
_cache: dict[tuple[int, int], tuple[float, IngestionMetrics]] = {}


def ingestion_metrics(
    session: Session,
    *,
    now: datetime | None = None,
    window_hours: int = 24,
    cache_seconds: int = 0,
) -> IngestionMetrics:
    """Poll success rate, ingestion lag and attachment counts (SPEC-01 Metrics, SPEC-10 F8).

    No query selects a message body: the previous row-by-row version took seconds and hundreds of
    megabytes at the 30,000-message scale SPEC-01 names as an edge case.
    """
    import time

    now = now or datetime.now(tz=UTC)
    if cache_seconds:
        key = (window_hours, int(now.timestamp()) // max(cache_seconds, 1))
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < cache_seconds:
            return hit[1]

    since = now - timedelta(hours=window_hours)
    m = IngestionMetrics(window_hours=window_hours)

    polls = session.execute(
        select(
            func.count(SourcePoll.id),
            func.coalesce(func.sum(SourcePoll.new), 0),
            func.coalesce(func.sum(SourcePoll.duplicates), 0),
            func.coalesce(func.sum(SourcePoll.skipped), 0),
        ).where(SourcePoll.started_at >= since)
    ).one()
    m.polls, m.messages_new, m.duplicates_linked, m.messages_skipped = (
        int(polls[0]),
        int(polls[1]),
        int(polls[2]),
        int(polls[3]),
    )
    m.polls_failed = int(
        session.scalar(
            select(func.count(SourcePoll.id)).where(
                SourcePoll.started_at >= since, SourcePoll.error_count > 0
            )
        )
        or 0
    )

    extracted, failed = _attachment_counts(session, since)
    m.attachments_extracted, m.attachments_failed = extracted, failed
    m.lag_p50_seconds, m.lag_p95_seconds, m.clock_skew = _lag(session, since)

    if cache_seconds:
        key = (window_hours, int(now.timestamp()) // max(cache_seconds, 1))
        _cache[key] = (time.monotonic(), m)
        if len(_cache) > 16:
            _cache.clear()
    return m


def _attachment_counts(session: Session, since: datetime) -> tuple[int, int]:
    extracted = (
        session.scalar(
            select(func.count(RawAttachment.id))
            .join(RawMessage, RawMessage.id == RawAttachment.message_id)
            .where(RawMessage.created_at >= since, RawAttachment.text.is_not(None))
        )
        or 0
    )
    failed = (
        session.scalar(
            select(func.count(RawAttachment.id))
            .join(RawMessage, RawMessage.id == RawAttachment.message_id)
            .where(
                RawMessage.created_at >= since,
                RawAttachment.text.is_(None),
                (RawAttachment.extraction_error.is_not(None))
                | (RawAttachment.large_document.is_(True))
                | (RawAttachment.oversize.is_(True)),
            )
        )
        or 0
    )
    return int(extracted), int(failed)


def _lag(session: Session, since: datetime) -> tuple[float | None, float | None, int]:
    """Ingestion lag percentiles. Postgres computes them in the database; elsewhere we sample.

    Either way only two timestamp columns are read, never a body.
    """
    skew = int(
        session.scalar(
            select(func.count(RawMessage.id)).where(
                RawMessage.created_at >= since, RawMessage.created_at < RawMessage.received_at
            )
        )
        or 0
    )
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        lag = func.extract("epoch", RawMessage.created_at - RawMessage.received_at)
        row = session.execute(
            select(
                func.percentile_cont(0.5).within_group(lag.asc()),
                func.percentile_cont(0.95).within_group(lag.asc()),
            ).where(RawMessage.created_at >= since, RawMessage.created_at >= RawMessage.received_at)
        ).one()
        p50 = float(row[0]) if row[0] is not None else None
        p95 = float(row[1]) if row[1] is not None else None
        return p50, p95, skew

    from bidtriage.core.clock import aware

    rows = session.execute(
        select(RawMessage.created_at, RawMessage.received_at)
        .where(RawMessage.created_at >= since)
        .order_by(RawMessage.created_at.desc())
        .limit(LAG_SAMPLE)
    ).all()
    lags: list[float] = []
    for created, received in rows:
        c, r = aware(created), aware(received)
        if c is not None and r is not None and c >= r:
            lags.append((c - r).total_seconds())
    return _percentile(lags, 0.5), _percentile(lags, 0.95), skew


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def pending_skips(session: Session, *, source_id: str | None = None) -> list[SourceSkip]:
    """Messages ingestion gave up on and nobody has reviewed yet (SPEC-10 F1)."""
    stmt = select(SourceSkip).where(SourceSkip.given_up.is_(True), SourceSkip.reviewed_at.is_(None))
    if source_id:
        stmt = stmt.where(SourceSkip.source_id == source_id)
    return list(session.scalars(stmt.order_by(SourceSkip.last_attempt_at.desc())).all())
