"""Worker process: scheduler tick every minute + job loop (ADR-002)."""

from __future__ import annotations

import logging
import time
import traceback

from bidtriage.core.db import session_scope
from bidtriage.core.jobs import claim, complete, fail
from bidtriage.worker.handlers import HANDLERS, Context, schedule_tick

log = logging.getLogger("bidtriage.worker")


def run_once(ctx: Context) -> bool:
    with session_scope() as session:
        job = claim(session)
        if job is None:
            return False
        handler = HANDLERS.get(job.kind)
        try:
            if handler is None:
                raise RuntimeError(f"no handler for {job.kind}")
            handler(session, job.payload, ctx)
            complete(session, job)
            log.info("job done kind=%s key=%s", job.kind, job.key)
        except Exception as e:  # noqa: BLE001
            session.rollback()
            with session_scope() as s2:
                j2 = s2.get(type(job), job.id)
                if j2 is not None:
                    fail(s2, j2, f"{e}\n{traceback.format_exc()}")
            log.exception("job failed kind=%s key=%s", job.kind, job.key)
        return True


def run_forever(ctx: Context, *, idle_sleep: float = 2.0) -> None:
    last_tick = 0.0
    while True:
        if time.time() - last_tick >= 60:
            with session_scope() as session:
                schedule_tick(session, ctx)
            last_tick = time.time()
        if not run_once(ctx):
            time.sleep(idle_sleep)
