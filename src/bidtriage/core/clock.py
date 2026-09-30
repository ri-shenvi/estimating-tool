"""Injectable clock so scheduling, timing factors and tokens are testable."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, **kwargs: float) -> None:
        from datetime import timedelta

        self._at = self._at + timedelta(**kwargs)


def to_business_tz(dt: datetime) -> datetime:
    return dt.astimezone(BUSINESS_TZ)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for timezone=True columns; treat naive as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
