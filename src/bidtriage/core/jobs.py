"""Postgres-backed job queue (ADR-002).

enqueue() is idempotent on `key`. claim() uses FOR UPDATE SKIP LOCKED on Postgres and a plain
UPDATE-with-check elsewhere (single-worker semantics, good enough for SQLite tests).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bidtriage.core.clock import Clock, SystemClock
from bidtriage.core.models import Job

DEFAULT_LEASE = timedelta(minutes=10)


class JobFailedError(Exception):
    """A handler failure whose already-written state must survive the retry.

    The job loop rolls the session back on an unexpected exception, which is right for a handler
    that failed halfway. A handler that has deliberately recorded the failure — a message marked
    `failed` so it reaches the digest's Needs review list — raises this instead, and the loop fails
    the job on the same session so both the state and the backoff commit together.
    """


def enqueue(
    session: Session,
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    clock: Clock | None = None,
) -> Job | None:
    """Insert a job unless one with the same key exists. Returns the new job or None if duplicate."""
    clock = clock or SystemClock()
    existing = session.scalar(select(Job).where(Job.key == key))
    if existing is not None:
        return None
    job = Job(
        kind=kind,
        key=key,
        payload=payload or {},
        run_at=run_at or clock.now(),
        priority=priority,
        max_attempts=max_attempts,
        created_at=clock.now(),
    )
    session.add(job)
    session.flush()
    return job


def claim(
    session: Session,
    kinds: list[str] | None = None,
    *,
    lease: timedelta = DEFAULT_LEASE,
    clock: Clock | None = None,
) -> Job | None:
    clock = clock or SystemClock()
    now = clock.now()
    stmt = (
        select(Job)
        .where(Job.status.in_(["pending", "leased"]))
        .where(Job.run_at <= now)
        .where((Job.leased_until.is_(None)) | (Job.leased_until < now))
        .order_by(Job.priority.asc(), Job.run_at.asc())
        .limit(1)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = session.scalar(stmt)
    if job is None:
        return None
    job.status = "leased"
    job.leased_until = now + lease
    job.attempts += 1
    session.flush()
    return job


def complete(session: Session, job: Job, *, clock: Clock | None = None) -> None:
    clock = clock or SystemClock()
    job.status = "done"
    job.finished_at = clock.now()
    job.leased_until = None
    session.flush()


def fail(
    session: Session,
    job: Job,
    error: str,
    *,
    backoff_base: timedelta = timedelta(seconds=30),
    clock: Clock | None = None,
) -> None:
    clock = clock or SystemClock()
    job.last_error = error[:4000]
    job.leased_until = None
    if job.attempts >= job.max_attempts:
        job.status = "failed"
        job.finished_at = clock.now()
    else:
        job.status = "pending"
        job.run_at = clock.now() + backoff_base * (2 ** (job.attempts - 1))
    session.flush()


def heartbeat(
    session: Session, job: Job, *, lease: timedelta = DEFAULT_LEASE, clock: Clock | None = None
) -> None:
    clock = clock or SystemClock()
    session.execute(update(Job).where(Job.id == job.id).values(leased_until=clock.now() + lease))
