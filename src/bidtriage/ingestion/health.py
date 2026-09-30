"""Source health thresholds (SPEC-01 F8). Pure: the caller supplies `now` and the last success.

One definition, used by the digest's System health section, the admin page and the alerting job, so
they can never disagree about whether a mailbox is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DEGRADED_AFTER = timedelta(minutes=30)
DOWN_AFTER = timedelta(minutes=60)

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
PAUSED = "paused"


@dataclass(frozen=True)
class SourceHealth:
    status: str
    detail: str = ""

    @property
    def is_down(self) -> bool:
        return self.status == DOWN


def source_health(
    *, last_success_at: datetime | None, now: datetime, paused: bool = False
) -> SourceHealth:
    if paused:
        return SourceHealth(PAUSED, "polling paused")
    if last_success_at is None:
        return SourceHealth(DOWN, "never polled successfully")
    age = now - last_success_at
    minutes = int(age.total_seconds() // 60)
    if age > DOWN_AFTER:
        return SourceHealth(DOWN, f"last success {minutes} min ago")
    if age > DEGRADED_AFTER:
        return SourceHealth(DEGRADED, f"last success {minutes} min ago")
    return SourceHealth(OK, f"last success {minutes} min ago")
