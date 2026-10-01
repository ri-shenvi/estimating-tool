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
from bidtriage.core.jobs import JobFailedError, enqueue
from bidtriage.core.models import Extraction, Job, Opportunity, RawMessage, Source, User
from bidtriage.extraction.geocode import CachedGeocoder, Geocoder, NominatimGeocoder
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
        geocoder: Geocoder | None = None,
    ) -> None:
        self._extractor = extractor
        self.sources = sources or {}
        self.blobs = blobs
        self.settings = get_settings()
        self._source_fingerprints: dict[str, str] = {}
        self._geocoder = geocoder
        # Set by the job loop for the duration of one handler call, so a handler can see which
        # attempt it is on (SPEC-02 F3 gives up after three).
        self.job: Job | None = None

    @property
    def extractor(self) -> Extractor:
        if self._extractor is None:
            raise RuntimeError("no extractor configured for this context")
        return self._extractor

    @property
    def attempt(self) -> int:
        return self.job.attempts if self.job is not None else 1

    @property
    def max_attempts(self) -> int:
        if self.job is not None:
            return self.job.max_attempts
        return self.settings.extraction_max_attempts

    def geocoder_for(self, session: Session) -> Geocoder | None:
        """Geocoder wrapped in the per-session cache; None when geocoding is switched off."""
        inner = self._geocoder
        if inner is None:
            if not self.settings.geocoder_url:
                return None
            inner = NominatimGeocoder(
                self.settings.geocoder_url,
                user_agent=self.settings.geocoder_user_agent,
                email=self.settings.geocoder_email,
                timeout=self.settings.geocoder_timeout_seconds,
            )
            self._geocoder = inner
        return CachedGeocoder(session, inner)

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
    _extract(session, payload, ctx, pipeline.extract_message)


def reextract_message(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    """Re-run extraction on one message, keeping the old record (SPEC-02 F4)."""
    _extract(session, payload, ctx, pipeline.reextract_message)


def _extract(
    session: Session,
    payload: dict[str, Any],
    ctx: Context,
    run: Callable[..., Any],
) -> None:
    msg = session.get(RawMessage, payload["message_id"])
    if msg is None:
        return
    try:
        run(
            session,
            msg,
            ctx.extractor,
            external_ref=pipeline.external_ref_for(session, msg),
            geocoder=ctx.geocoder_for(session),
            attempt=ctx.attempt,
            max_attempts=ctx.max_attempts,
        )
    except Exception as e:  # noqa: BLE001
        # The message is now `retrying` or `failed`; that write has to survive the retry.
        raise JobFailedError(f"extraction failed for {msg.id}: {e}") from e


def retry_extractions(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    """Re-queue failed extractions so an API outage heals without anyone noticing (SPEC-02 F3)."""
    queued = pipeline.enqueue_extraction_retries(
        session, max_rounds=ctx.settings.extraction_max_retry_rounds
    )
    if queued:
        log.info("re-queued %d failed extraction(s)", queued)


def reextract_stale_prompts(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    """After a prompt bump, walk recent messages back through extraction (SPEC-02 F4)."""
    queued = pipeline.enqueue_stale_prompt_reextractions(
        session, window_days=ctx.settings.reextraction_window_days
    )
    if queued:
        log.info("queued %d re-extraction(s) for the current prompt version", queued)


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
    """Rescore the live board (SPEC-04 F8). Runs nightly, and again when a profile is activated.

    Only an activation marks band changes for the digest: a job whose band moved because the
    calendar advanced past a timing boundary has not changed, and saying it did would train the
    chief estimator to ignore the "Changed" line.
    """
    note = (
        f"Rescored under profile v{payload['profile_version']}"
        if payload.get("profile_version")
        else None
    )
    n = pipeline.rescore(
        session,
        session.scalars(
            select(Opportunity).where(
                Opportunity.archived_at.is_(None),
                Opportunity.status.not_in(["submitted", "won", "lost"]),
            )
        ).all(),
        home=ctx.home,
        note_band_change=note,
    )
    log.info(
        "rescored %d opportunit%s%s", n, "y" if n == 1 else "ies", f" ({note})" if note else ""
    )


def rescore_gc(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    pipeline.rescore(
        session,
        session.scalars(
            select(Opportunity).where(
                Opportunity.gc_id == payload["gc_id"], Opportunity.archived_at.is_(None)
            )
        ).all(),
        home=ctx.home,
    )


def unsnooze(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    now = datetime.now(tz=UTC)
    for opp in session.scalars(
        select(Opportunity).where(Opportunity.status == "snoozed", Opportunity.snooze_until <= now)
    ).all():
        if pipeline.set_status(session, opp, "undecided", at=now):
            opp.snooze_until = None
            opp.changed_since_digest = True
            opp.change_summary = "snooze ended"


def archive_stale(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    """Retire opportunities nobody bid and nothing touched for 180 days (SPEC-03 F5)."""
    archived = pipeline.archive_stale(
        session, now=datetime.now(tz=UTC), after_days=ctx.settings.opportunity_archive_days
    )
    if archived:
        log.info("archived %d stale opportunit%s", archived, "y" if archived == 1 else "ies")


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
    enqueue(session, "archive_stale", f"archive_stale:{now.strftime('%Y%m%d')}", {}, priority=97)
    enqueue(
        session,
        "retry_extractions",
        f"retry_extractions:{now.strftime('%Y%m%d%H')}",
        {},
        priority=96,
    )
    enqueue(
        session,
        "reextract_stale",
        f"reextract_stale:{now.strftime('%Y%m%d')}",
        {},
        priority=pipeline.REEXTRACT_PRIORITY,
    )
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
    "reextract_message": reextract_message,
    "retry_extractions": retry_extractions,
    "reextract_stale": reextract_stale_prompts,
    "resolve_message": resolve_message,
    "score_opportunity": score_opportunity,
    "rescore_all": rescore_all,
    "rescore_gc": rescore_gc,
    "unsnooze": unsnooze,
    "archive_stale": archive_stale,
    "digest": build_and_send_digest,
}
