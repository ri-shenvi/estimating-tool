"""Job handlers keyed by kind (system-design §6)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.blobs import BlobStore
from bidtriage.core.config import get_settings
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import Extraction, Job, Opportunity, RawMessage, Source, User
from bidtriage.extraction.protocol import Extractor
from bidtriage.worker import ingest_job, pipeline

log = logging.getLogger("bidtriage.worker")

Handler = Callable[[Session, dict[str, Any], "Context"], None]


class Context:
    def __init__(
        self,
        extractor: Extractor | None,
        sources: dict[str, Any] | None = None,
        blobs: BlobStore | None = None,
    ) -> None:
        self._extractor = extractor
        self.sources = sources or {}
        self.blobs = blobs
        self.settings = get_settings()
        self._source_fingerprints: dict[str, str] = {}

    @property
    def extractor(self) -> Extractor:
        if self._extractor is None:
            raise RuntimeError("no extractor configured for this context")
        return self._extractor

    @property
    def home(self) -> tuple[float, float]:
        return (self.settings.home_lat, self.settings.home_lon)

    def source_impl(self, src: Source) -> Any:
        """Registered implementation if a test injected one, else built from the row's config.

        The cache is keyed on a fingerprint of `config_enc`, so rotating a client secret or app
        password takes effect on the next poll instead of needing a worker restart (SPEC-10 F3).
        """
        from bidtriage.worker.sources import build_source, config_fingerprint

        impl = self.sources.get(src.id)
        fingerprint = config_fingerprint(src)
        if impl is not None and self._source_fingerprints.get(src.id) in (None, fingerprint):
            # None means a test injected this implementation directly; leave it alone.
            return impl
        impl = build_source(src, self.settings.secret_key)
        self.sources[src.id] = impl
        self._source_fingerprints[src.id] = fingerprint
        return impl


def _source_impl(session: Session, payload: dict[str, Any], ctx: Context) -> tuple[Source, Any]:
    src = session.get(Source, payload["source_id"])
    if src is None:
        raise LookupError(f"no such source {payload['source_id']}")
    return src, ctx.source_impl(src)


def poll_source(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    src = session.get(Source, payload["source_id"])
    if src is None or src.paused:
        return
    src, impl = _source_impl(session, payload, ctx)
    ingest_job.run_poll(session, src, impl, ctx)


def backfill_source(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    src = session.get(Source, payload["source_id"])
    if src is None or src.paused or src.backfill_done:
        return
    src, impl = _source_impl(session, payload, ctx)
    ingest_job.run_backfill(session, src, impl, ctx)


def check_sources(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    ingest_job.check_sources(session, ctx)


def ingest_maintenance(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    """Close polls abandoned by a dead worker and prune old poll history (SPEC-10 F6)."""
    closed = ingest_job.close_abandoned_polls(session)
    pruned = ingest_job.prune_polls(session, retention_days=ctx.settings.ingest_poll_retention_days)
    if closed or pruned:
        log.info("ingest maintenance: closed=%d pruned=%d", closed, pruned)


def extract_message(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    msg = session.get(RawMessage, payload["message_id"])
    if msg is None:
        return
    from bidtriage.core.models import MessageSource

    link = session.scalar(select(MessageSource).where(MessageSource.message_id == msg.id))
    ref = (
        link.provider_message_id.rsplit(".", 1)[0]
        if link and link.provider_message_id.endswith(".eml")
        else None
    )
    pipeline.extract_message(session, msg, ctx.extractor, external_ref=ref)


def resolve_message(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    msg = session.get(RawMessage, payload["message_id"])
    ext = session.get(Extraction, payload["extraction_id"])
    if msg is None or ext is None:
        return
    pipeline.resolve_message(session, msg, ext)


def score_opportunity(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    opp = session.get(Opportunity, payload["opportunity_id"])
    if opp is None:
        return
    pipeline.score_opportunity(session, opp, home=ctx.home)


def rescore_all(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    profile, version = pipeline.active_profile(session)
    for opp in session.scalars(
        select(Opportunity).where(
            Opportunity.archived_at.is_(None),
            Opportunity.status.not_in(["submitted", "won", "lost"]),
        )
    ).all():
        pipeline.score_opportunity(
            session, opp, home=ctx.home, profile=profile, profile_version=version
        )


def rescore_gc(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    profile, version = pipeline.active_profile(session)
    for opp in session.scalars(
        select(Opportunity).where(
            Opportunity.gc_id == payload["gc_id"], Opportunity.archived_at.is_(None)
        )
    ).all():
        pipeline.score_opportunity(
            session, opp, home=ctx.home, profile=profile, profile_version=version
        )


def unsnooze(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    now = datetime.now(tz=UTC)
    for opp in session.scalars(
        select(Opportunity).where(Opportunity.status == "snoozed", Opportunity.snooze_until <= now)
    ).all():
        opp.status = "undecided"
        opp.snooze_until = None
        opp.changed_since_digest = True
        opp.change_summary = "snooze ended"


def build_and_send_digest(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    from bidtriage.worker.digest_job import build_and_send

    build_and_send(
        session,
        ctx,
        date=payload.get("date"),
        recipient_id=payload.get("recipient_id"),
        send=payload.get("send", True),
    )


def _backfill_outstanding(session: Session, source_id: str) -> bool:
    """True when a backfill batch for this source is already queued or running."""
    return (
        session.scalar(
            select(Job.id)
            .where(
                Job.kind == "backfill_source",
                Job.status.in_(["pending", "leased"]),
                Job.key.like(f"backfill:{source_id}:%"),
            )
            .limit(1)
        )
        is not None
    )


def schedule_tick(session: Session, ctx: Context, now: datetime | None = None) -> None:
    """Called every minute by the scheduler; enqueues idempotent jobs with minute-bucket keys."""
    now = now or datetime.now(tz=UTC)
    bucket = now.strftime("%Y%m%d%H%M")
    five = now.strftime("%Y%m%d%H") + str(now.minute // 5)
    for src in session.scalars(select(Source).where(Source.paused.is_(False))).all():
        enqueue(session, "poll_source", f"poll:{src.id}:{five}", {"source_id": src.id}, priority=50)
        if (
            not src.backfill_done
            and not src.backfill_stuck
            and not _backfill_outstanding(session, src.id)
        ):
            # Chaining inside run_backfill is the fast path; this tick starts the walk on first
            # connection and recovers a chain broken by a job that exhausted its retries
            # (SPEC-10 F5). Skipped while a batch is still queued so one source never has two.
            enqueue(
                session,
                "backfill_source",
                f"backfill:{src.id}:tick:{five}",
                {"source_id": src.id},
                priority=ingest_job.BACKFILL_PRIORITY,
            )
    enqueue(session, "unsnooze", f"unsnooze:{five}", {}, priority=90)
    enqueue(session, "check_sources", f"check_sources:{five}", {}, priority=40)
    enqueue(
        session,
        "ingest_maintenance",
        f"ingest_maintenance:{now.strftime('%Y%m%d%H')}",
        {},
        priority=95,
    )
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(ctx.settings.digest_timezone))
    hh, mm = (int(x) for x in ctx.settings.digest_time.split(":"))
    build_at = (
        local.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(minutes=10)
    ).time()
    if local.weekday() < 5 and local.hour == build_at.hour and local.minute == build_at.minute:
        enqueue(
            session, "rescore_all", f"rescore:nightly:{local.date().isoformat()}", {}, priority=10
        )
        for u in session.scalars(select(User).where(User.active.is_(True))).all():
            enqueue(
                session,
                "digest",
                f"digest:{local.date().isoformat()}:{u.id}",
                {"date": local.date().isoformat(), "recipient_id": u.id, "send": True},
                priority=20,
                max_attempts=12,
                run_at=now + timedelta(minutes=2),
            )
    _ = bucket


HANDLERS: dict[str, Handler] = {
    "poll_source": poll_source,
    "backfill_source": backfill_source,
    "check_sources": check_sources,
    "ingest_maintenance": ingest_maintenance,
    "extract_message": extract_message,
    "resolve_message": resolve_message,
    "score_opportunity": score_opportunity,
    "rescore_all": rescore_all,
    "rescore_gc": rescore_gc,
    "unsnooze": unsnooze,
    "digest": build_and_send_digest,
}
