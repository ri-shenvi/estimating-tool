from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from bidtriage.core.clock import BUSINESS_TZ, FrozenClock
from bidtriage.core.models import Base

FIXTURES = Path(__file__).parent / "fixtures" / "messages"
NOW = datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
