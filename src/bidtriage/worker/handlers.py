"""Job handlers keyed by kind (system-design §6)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.config import get_settings
from bidtriage.core.jobs import enqueue
from bidtriage.core.models import Extraction, Opportunity, RawMessage, Source, SourcePoll, User
from bidtriage.extraction.protocol import Extractor
from bidtriage.worker import pipeline

Handler = Callable[[Session, dict[str, Any], "Context"], None]


class Context:
    def __init__(self, extractor: Extractor | None, sources: dict[str, Any] | None = None) -> None:
        self._extractor = extractor
        self.sources = sources or {}
        self.settings = get_settings()

    @property
    def extractor(self) -> Extractor:
        if self._extractor is None:
            raise RuntimeError("no extractor configured for this context")
        return self._extractor

    @property
    def home(self) -> tuple[float, float]:
        return (self.settings.home_lat, self.settings.home_lon)


def poll_source(session: Session, payload: dict[str, Any], ctx: Context) -> None:
    src = session.get(Source, payload["source_id"])
    if src is None or src.paused:
        return
    impl = ctx.sources.get(src.id)
    if impl is None:
        raise RuntimeError(f"no source implementation registered for {src.id}")
    poll = SourcePoll(source_id=src.id, started_at=datetime.now(tz=UTC))
    session.add(poll)
    res = impl.poll(src.delta_state)
    new = dupes = 0
    for provider_id, parsed in res.messages:
        _, is_new = pipeline.ingest_parsed(
            session, source_id=src.id, provider_message_id=provider_id, parsed=parsed
        )
        new += int(is_new)
        dupes += int(not is_new)
    src.delta_state = res.new_state
    if not res.errors:
        src.last_success_at = datetime.now(tz=UTC)
    poll.finished_at, poll.seen, poll.new, poll.duplicates, poll.errors = (
        datetime.now(tz=UTC),
        len(res.messages),
        new,
        dupes,
        res.errors,
    )


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


def schedule_tick(session: Session, ctx: Context, now: datetime | None = None) -> None:
    """Called every minute by the scheduler; enqueues idempotent jobs with minute-bucket keys."""
    now = now or datetime.now(tz=UTC)
    bucket = now.strftime("%Y%m%d%H%M")
    five = now.strftime("%Y%m%d%H") + str(now.minute // 5)
    for src in session.scalars(select(Source).where(Source.paused.is_(False))).all():
        enqueue(session, "poll_source", f"poll:{src.id}:{five}", {"source_id": src.id}, priority=50)
    enqueue(session, "unsnooze", f"unsnooze:{five}", {}, priority=90)
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
    "extract_message": extract_message,
    "resolve_message": resolve_message,
    "score_opportunity": score_opportunity,
    "rescore_all": rescore_all,
    "rescore_gc": rescore_gc,
    "unsnooze": unsnooze,
    "digest": build_and_send_digest,
}
