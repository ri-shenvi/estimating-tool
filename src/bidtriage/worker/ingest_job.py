"""Polling orchestration for SPEC-01 F7 (scheduling and state) and F8 (health).

Two entry points, `run_poll` for live mail and `run_backfill` for the first-connection history walk,
plus `check_sources` which turns the health thresholds into a once-per-outage admin alert. Each
message is committed on its own so a poll that dies mid-batch leaves the messages it already stored
and the rerun finishes the rest without duplicates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.blobs import BlobStore
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import Source, SourcePoll, User
from bidtriage.ingestion.health import DOWN, SourceHealth, source_health
from bidtriage.ingestion.protocol import PollResult
from bidtriage.worker import pipeline

if TYPE_CHECKING:  # pragma: no cover
    from bidtriage.worker.handlers import Context

log = logging.getLogger("bidtriage.ingest")

BACKFILL_PRIORITY = 80
"""Jobs are claimed in ascending priority, so 80 always yields to a due poll_source at 50 and a
backfill can never starve live mail (SPEC-01 F7)."""


class Mailer(Protocol):
    def __call__(self, *, to: str, subject: str, html: str, text: str) -> str: ...


@dataclass
class PollSummary:
    seen: int = 0
    new: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    more_available: bool = False


def run_poll(
    session: Session,
    src: Source,
    impl: Any,
    ctx: Context,
    *,
    mode: str = "live",
    limit: int | None = None,
) -> PollSummary:
    """Fetch one batch from `src` and store it. Writes a `source_poll` row either way (F7)."""
    started = datetime.now(tz=UTC)
    poll = SourcePoll(source_id=src.id, mode=mode, started_at=started, errors=[])
    session.add(poll)
    session.commit()  # the poll row must survive a crash inside the batch

    limit = limit or ctx.settings.ingest_live_batch
    if mode == "live":
        _seed_live_cursor(session, src, impl)
    try:
        if mode == "backfill":
            since = datetime.now(tz=UTC) - timedelta(days=src.backfill_days)
            res: PollResult = impl.backfill(src.delta_state, since=since, limit=limit)
        else:
            res = impl.poll(src.delta_state, limit=limit)
    except Exception as e:
        # The mailbox could not be reached at all: this is what makes a source degrade and then go
        # down (SPEC-01 F8), so `last_success_at` is deliberately left alone.
        _finish(session, poll, PollSummary(errors=[f"poll failed: {e}"]))
        src.status = "error"
        session.commit()
        raise

    summary = _store_batch(session, src, res, ctx)
    src.delta_state = res.new_state
    # We reached the mailbox, so the poll succeeded even if individual messages were unreadable;
    # otherwise one poison message would eventually report a healthy mailbox as down.
    src.last_success_at = datetime.now(tz=UTC)
    src.status = "warning" if summary.errors else "active"
    _finish(session, poll, summary)
    session.commit()
    return summary


def _seed_live_cursor(session: Session, src: Source, impl: Any) -> None:
    """Point a brand-new source's live cursor at *now* so history is the backfill's job (F7).

    Without this the first delta query or UID scan would drain the whole folder, ignoring the
    configured backfill window. A source with `backfill_days=0` is opted out and drains normally.
    """
    if src.delta_state or src.backfill_days <= 0 or not hasattr(impl, "seed"):
        return
    src.delta_state = impl.seed({})
    session.commit()


def _store_batch(session: Session, src: Source, res: PollResult, ctx: Context) -> PollSummary:
    summary = PollSummary(
        seen=len(res.messages), errors=list(res.errors), more_available=res.more_available
    )
    blobs = _blobs(ctx)
    ocr = ctx.settings.ingest_ocr
    for provider_id, parsed in res.messages:
        try:
            _, is_new = pipeline.ingest_parsed(
                session,
                source_id=src.id,
                provider_message_id=provider_id,
                parsed=parsed,
                recipient_path=src.mailbox or src.name,
                ocr=ocr,
                blobs=blobs,
            )
            session.commit()
        except Exception as e:  # noqa: BLE001 - one poison message must not block the mailbox
            session.rollback()
            summary.errors.append(f"{provider_id}: {e}")
            log.exception("ingest failed source=%s provider_id=%s", src.id, provider_id)
            continue
        summary.new += int(is_new)
        summary.duplicates += int(not is_new)
    return summary


def _blobs(ctx: Context) -> BlobStore | None:
    blobs = getattr(ctx, "blobs", None)
    if blobs is not None:
        return blobs
    from bidtriage.core.blobs import get_blob_store

    return get_blob_store()


def _finish(session: Session, poll: SourcePoll, summary: PollSummary) -> None:
    poll.finished_at = datetime.now(tz=UTC)
    poll.seen, poll.new, poll.duplicates = summary.seen, summary.new, summary.duplicates
    poll.errors = list(summary.errors)
    session.flush()


def run_backfill(session: Session, src: Source, impl: Any, ctx: Context) -> PollSummary:
    """One backfill batch, oldest-first. Chains the next batch behind live polls (SPEC-01 F7)."""
    if src.backfill_done:
        return PollSummary()
    if not hasattr(impl, "backfill"):
        src.backfill_done = True
        session.flush()
        return PollSummary()
    summary = run_poll(
        session, src, impl, ctx, mode="backfill", limit=ctx.settings.ingest_backfill_batch
    )
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
    else:
        src.backfill_done = True
    session.commit()
    return summary


# ---------------------------------------------------------------------- health (F8)


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
    for to in recipients:
        try:
            send(to=to, subject=subject, html=f"<p>{text}</p>", text=text)
        except Exception:  # noqa: BLE001 - a broken SMTP must not fail the health job
            log.exception("could not send source-down alert to %s", to)
            return False
    return True


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


# ---------------------------------------------------------------------- metrics


@dataclass
class IngestionMetrics:
    """The SPEC-01 metrics, over a trailing window."""

    window_hours: int
    polls: int = 0
    polls_failed: int = 0
    messages_new: int = 0
    duplicates_linked: int = 0
    attachments_extracted: int = 0
    attachments_failed: int = 0
    lag_p50_seconds: float | None = None
    lag_p95_seconds: float | None = None

    @property
    def poll_success_rate(self) -> float | None:
        return None if not self.polls else (self.polls - self.polls_failed) / self.polls


def ingestion_metrics(
    session: Session, *, now: datetime | None = None, window_hours: int = 24
) -> IngestionMetrics:
    """Poll success rate, ingestion lag and attachment extraction counts (SPEC-01 Metrics)."""
    from bidtriage.core.clock import aware
    from bidtriage.core.models import RawAttachment, RawMessage

    now = now or datetime.now(tz=UTC)
    since = now - timedelta(hours=window_hours)
    m = IngestionMetrics(window_hours=window_hours)

    for poll in session.scalars(select(SourcePoll).where(SourcePoll.started_at >= since)).all():
        m.polls += 1
        m.polls_failed += int(bool(poll.errors))
        m.messages_new += poll.new or 0
        m.duplicates_linked += poll.duplicates or 0

    lags: list[float] = []
    messages = session.scalars(select(RawMessage).where(RawMessage.created_at >= since)).all()
    for msg in messages:
        created, received = aware(msg.created_at), aware(msg.received_at)
        if created and received and created >= received:
            lags.append((created - received).total_seconds())
    m.lag_p50_seconds, m.lag_p95_seconds = _percentile(lags, 0.5), _percentile(lags, 0.95)

    ids = [msg.id for msg in messages]
    if ids:
        for att in session.scalars(
            select(RawAttachment).where(RawAttachment.message_id.in_(ids))
        ).all():
            if att.text is not None:
                m.attachments_extracted += 1
            elif att.extraction_error or att.large_document or att.oversize:
                m.attachments_failed += 1
    return m


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]
