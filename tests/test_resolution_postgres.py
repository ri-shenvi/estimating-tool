"""SPEC-03 behaviour that only PostgreSQL can show (technical notes: trigram candidate search).

Everything here is skipped without a server. Run it with `make test-postgres`, or point
`BIDTRIAGE_TEST_POSTGRES_URL` at any PostgreSQL that has `pg_trgm` available.

These are not duplicates of the SQLite tests. They cover the three things `Base.metadata` does not
carry and SQLite cannot express: the JSONB variants, the partial unique index, and the GIN trigram
index — plus the one bug that only an execution plan reveals, which is an index that exists and is
never used.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from bidtriage.core.models import Opportunity
from bidtriage.resolution import Incoming, normalize_name
from bidtriage.worker import pipeline

pytestmark = pytest.mark.postgres

PROBE = "benedum hall laboratory fourth floor"


def _seed(session, names: list[str]) -> dict[str, str]:
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    ids = {}
    for name in names:
        o = Opportunity(
            status="new",
            canonical={"project_name": {"value": name, "confidence": 0.9}, "scope_items": []},
            normalized_name=normalize_name(name),
            first_seen_at=now,
            last_activity_at=now,
        )
        session.add(o)
        session.flush()
        ids[name] = o.id
    return ids


def test_migration_creates_the_postgres_only_schema(pg_session):  # type: ignore[no-untyped-def]
    """JSONB variants, the SPEC-10 partial unique index and the SPEC-03 trigram index."""
    kinds = dict(
        pg_session.execute(
            text(
                "select table_name || '.' || column_name, data_type "
                "from information_schema.columns "
                "where table_schema = 'public' and column_name in ('canonical', 'before')"
            )
        ).all()
    )
    assert kinds["opportunities.canonical"] == "jsonb"
    assert kinds["merge_log.before"] == "jsonb"

    defs = dict(
        pg_session.execute(
            text("select indexname, indexdef from pg_indexes where schemaname = 'public'")
        ).all()
    )
    assert "gin (normalized_name gin_trgm_ops)" in defs["ix_opportunities_normalized_name_trgm"]
    assert "WHERE (internet_message_id IS NOT NULL)" in defs["uq_raw_messages_internet_message_id"]
    assert "ix_opportunity_keys_lookup" in defs and "ix_opportunities_geohash" in defs


def test_pg_trgm_is_detected(pg_session):  # type: ignore[no-untyped-def]
    assert pipeline._has_pg_trgm(pg_session) is True
    assert getattr(pg_session.get_bind(), "_bidtriage_pg_trgm", None) is True


def test_name_candidates_use_the_trigram_index(pg_session):  # type: ignore[no-untyped-def]
    """The regression guard for a real defect: an index that exists and is never used.

    `gin_trgm_ops` indexes the `%` operator. `similarity(a, b) > x` is a function call in the
    predicate and is not indexable at all — with that form the planner finds no index plan even
    with `enable_seqscan` off, so the index costs writes and buys nothing.
    """
    _seed(pg_session, [f"Some Job Number {n}" for n in range(20)])
    # The question is whether an index plan is *possible* for this query shape, not whether the
    # planner picks one on a small table, so sequential scans are taken away first.
    pg_session.execute(text("SET LOCAL enable_seqscan = off"))

    indexable = _explain(pg_session, Opportunity.normalized_name.op("%", is_comparison=True)(PROBE))
    assert "ix_opportunities_normalized_name_trgm" in indexable, indexable
    assert "Bitmap Index Scan" in indexable, indexable

    # The form this used to emit. The planner falls back to a scan it was explicitly told not to
    # use, because the index cannot serve a function call in the predicate.
    not_indexable = _explain(
        pg_session,
        func.similarity(Opportunity.normalized_name, PROBE) > pipeline.NAME_TRIGRAM_FLOOR,
    )
    assert "ix_opportunities_normalized_name_trgm" not in not_indexable, not_indexable
    assert "Seq Scan" in not_indexable, not_indexable


def _explain(session, clause) -> str:  # type: ignore[no-untyped-def]
    stmt = select(Opportunity.id).where(Opportunity.archived_at.is_(None), clause)
    sql = str(stmt.compile(session.get_bind(), compile_kwargs={"literal_binds": True}))
    # SQLAlchemy doubles `%` for the DBAPI paramstyle; EXPLAIN takes the statement verbatim.
    return "\n".join(
        row[0] for row in session.execute(text("EXPLAIN " + sql.replace("%%", "%"))).all()
    )


def test_percent_operator_matches_the_similarity_floor(pg_session):  # type: ignore[no-untyped-def]
    """`%` reads its threshold from a GUC, so the two forms must still select the same rows."""
    _seed(
        pg_session,
        [
            "Benedum Hall Lab Renovation",
            "Pittsburgh Distribution Center",
            "Hazelwood Green Parcel C Parking Structure",
        ],
    )
    pg_session.execute(
        text(f"SET LOCAL pg_trgm.similarity_threshold = {pipeline.NAME_TRIGRAM_FLOOR}")
    )
    via_operator = set(
        pg_session.scalars(
            select(Opportunity.id).where(
                Opportunity.normalized_name.op("%", is_comparison=True)(PROBE)
            )
        ).all()
    )
    via_function = set(
        pg_session.scalars(
            select(Opportunity.id).where(
                func.similarity(Opportunity.normalized_name, PROBE) >= pipeline.NAME_TRIGRAM_FLOOR
            )
        ).all()
    )
    assert via_operator == via_function and via_operator


def test_threshold_does_not_leak_out_of_the_transaction(pg_session):  # type: ignore[no-untyped-def]
    """`SET LOCAL`, so a pooled connection is not left with our threshold (SPEC-03 F2)."""
    default = pg_session.scalar(text("SHOW pg_trgm.similarity_threshold"))
    _seed(pg_session, ["Benedum Hall Lab Renovation"])
    pg_session.begin_nested()
    pipeline.candidate_ids(pg_session, _incoming("Benedum Hall Laboratory Fourth Floor"), set())
    assert pg_session.scalar(text("SHOW pg_trgm.similarity_threshold")) == str(
        pipeline.NAME_TRIGRAM_FLOOR
    )
    pg_session.rollback()
    assert pg_session.scalar(text("SHOW pg_trgm.similarity_threshold")) == default


def _incoming(name: str) -> Incoming:
    return Incoming(
        project_name=name,
        gc_id=None,
        gc_domain=None,
        city=None,
        lat=None,
        lon=None,
        bid_due=None,
        owner_name=None,
    )


def test_trigram_finds_renames_and_is_more_selective_than_the_fallback(pg_session):  # type: ignore[no-untyped-def]
    """Recall no worse than the SQLite fallback, precision better (SPEC-03 edge cases: rename)."""
    ids = _seed(
        pg_session,
        [
            "Benedum Hall Lab Renovation",
            "Duquesne University Residence Hall Upgrade",
            "Etna Borough Pump Station Upgrade",
            "Carnegie Library Woods Run Branch",
        ],
    )
    incoming = _incoming("Benedum Hall Laboratory Renovation, Fourth Floor")

    # Fallback first: the trigram branch issues a `SET LOCAL`, and undoing that would mean a
    # rollback, which would take the seeded rows with it.
    original = pipeline._has_pg_trgm
    try:
        pipeline._has_pg_trgm = lambda _s: False  # type: ignore[assignment]
        fallback = set(pipeline.candidate_ids(pg_session, incoming, set()))
    finally:
        pipeline._has_pg_trgm = original  # type: ignore[assignment]
    trigram = set(pipeline.candidate_ids(pg_session, incoming, set()))

    target = ids["Benedum Hall Lab Renovation"]
    assert target in trigram, "the trigram pre-filter must not lose the real match"
    assert target in fallback
    # "Upgrade" and "Hall" are shared tokens, so LIKE drags in projects trigram correctly rejects.
    assert len(trigram) <= len(fallback)


def test_candidate_generation_still_stops_at_hard_keys_with_nothing_to_match(pg_session):  # type: ignore[no-untyped-def]
    """The empty-`or_()` guard, on the dialect where a full scan would actually hurt."""
    _seed(pg_session, [f"Distinct Job Number {n}" for n in range(5)])
    nothing = _incoming(None)  # type: ignore[arg-type]
    assert pipeline.candidate_ids(pg_session, nothing, set()) == []


def test_candidate_generation_emits_the_indexable_operator(pg_session):  # type: ignore[no-untyped-def]
    """Guards the regression directly: what `candidate_ids` sends, not what it could have sent.

    Without this, reverting the name clause to `similarity(a, b) > x` would leave every other test
    green — the results are identical, only the execution plan changes.
    """
    from sqlalchemy import event

    _seed(pg_session, ["Benedum Hall Lab Renovation"])
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    bind = pg_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        pipeline.candidate_ids(pg_session, _incoming("Benedum Hall Laboratory Fourth Floor"), set())
    finally:
        event.remove(bind, "before_cursor_execute", record)

    name_query = next(s for s in statements if "normalized_name" in s)
    assert "similarity(" not in name_query, name_query
    assert "%" in name_query, name_query
    assert any("pg_trgm.similarity_threshold" in s for s in statements), statements


def test_a_failed_optional_statement_does_not_poison_the_migration(pg_session):  # type: ignore[no-untyped-def]
    """Why migration 0003 wraps `CREATE EXTENSION` in a savepoint rather than a bare try/except.

    `migrations/env.py` runs every revision inside one transaction. PostgreSQL aborts the whole
    transaction on any error, so catching the Python exception is not enough — every later
    statement, including the version stamp, fails with "current transaction is aborted". A
    savepoint keeps the failure local, which is what makes the documented fallback to token search
    actually reachable for a role that cannot install the extension.
    """
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with pg_session.begin_nested():
            pg_session.execute(text("CREATE EXTENSION no_such_extension_exists"))
    pg_session.rollback()
    # The connection is still usable, which is the whole point: without the savepoint the rest of
    # the revision — including the version stamp — would fail with "transaction is aborted".
    assert pg_session.scalar(text("SELECT 1")) == 1
    assert pg_session.scalar(text("SELECT count(*) FROM opportunities")) == 0


def test_migration_0003_guards_both_optional_statements():  # type: ignore[no-untyped-def]
    """A static check, because the unprivileged-role path cannot be created from inside the suite."""
    import pathlib

    source = pathlib.Path("migrations/versions/0003_spec03_resolution.py").read_text()
    body = source[source.index("def upgrade(") : source.index("def downgrade(")]
    assert body.count("begin_nested()") == 2, "both optional statements need their own savepoint"
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in body
    assert "gin_trgm_ops" in body
