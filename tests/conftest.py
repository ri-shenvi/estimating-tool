from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from bidtriage.core.clock import BUSINESS_TZ, FrozenClock
from bidtriage.core.models import GC, Base, GCDomain

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


@pytest.fixture
def pjdick(session: Session) -> GC:
    """A GC in the directory with a known domain: what SPEC-03 matching resolves against."""
    row = GC(canonical_name="PJ Dick", kind="gc", created_from="seed", created_at=NOW)
    session.add(row)
    session.flush()
    session.add(GCDomain(gc_id=row.id, domain="pjdick.com"))
    session.flush()
    return row


# --------------------------------------------------------------- PostgreSQL

#: Where the `postgres`-marked tests look for a server. Defaults to the same credentials
#: `infra/docker-compose.yml` and `core/config.py` use, so a local install or the compose service
#: both work with no configuration.
PG_ADMIN_URL = os.environ.get(
    "BIDTRIAGE_TEST_POSTGRES_URL",
    "postgresql+psycopg://bidtriage:bidtriage@localhost:5432/postgres",
)
PG_TEST_DB = "bidtriage_pytest"


def _pg_admin_engine():
    """An engine on the maintenance database, or None when no server is reachable."""
    from sqlalchemy import create_engine

    try:
        engine = create_engine(
            PG_ADMIN_URL, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
        )
        with engine.connect():
            return engine
    except Exception:  # noqa: BLE001 - absence of a server is the normal case
        return None


@pytest.fixture(scope="session")
def pg_engine():
    """A PostgreSQL database at migration head, or a skip when no server is available.

    Built with alembic rather than `Base.metadata`, because what these tests are checking is
    precisely the things metadata does not carry: the trigram index, the partial unique index and
    the JSONB variants (SPEC-03 technical notes).
    """
    from sqlalchemy import create_engine, text

    admin = _pg_admin_engine()
    if admin is None:
        pytest.skip(f"no PostgreSQL at {PG_ADMIN_URL.rsplit('@', 1)[-1]}")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {PG_TEST_DB}"))
        conn.execute(text(f"CREATE DATABASE {PG_TEST_DB}"))
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + f"/{PG_TEST_DB}"

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    engine = create_engine(url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {PG_TEST_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def pg_session(pg_engine) -> Iterator[Session]:
    """A session whose work is rolled back, so the tests share one migrated database."""
    conn = pg_engine.connect()
    trans = conn.begin()
    s = sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")()
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        conn.close()
