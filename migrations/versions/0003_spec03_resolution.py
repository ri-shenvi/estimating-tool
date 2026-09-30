"""SPEC-03: hard-key index, addendum copies, merge log, trigram candidate search.

Revision ID: 0003_spec03_resolution
Revises: 0002_spec02_extraction
Create Date: 2026-09-30

`opportunity_keys` denormalizes the hard keys (email thread, platform project id) out of the source
messages, so matching an inbound message is one indexed lookup instead of a walk over every
candidate's messages and links — the difference between the SPEC-03 2 s budget and a table scan.
`merge_log` holds the manual merges and splits together with the state undo restores. The two new
`opportunities` columns are the geocode bucket candidate generation filters on and the
"big enough to tell someone who already passed" marker from F5, and `addenda.copies` counts a CC
fan-out as one addendum.

On PostgreSQL this also installs `pg_trgm` and the GIN index behind the trigram name search. Both
are optional: `CREATE EXTENSION` needs a privileged role, and a deployment that cannot have it
falls back to token matching (see `pipeline._has_pg_trgm`), so failure here is logged, not fatal.

Guarded by inspection throughout, because `0001_baseline` builds from `Base.metadata`: on a database
created after this change the tables already exist and this only moves the version stamp.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0003_spec03_resolution"
down_revision = "0002_spec02_extraction"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.spec03")


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in _inspector().get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    tables = _tables()
    if "opportunity_keys" not in tables:
        op.create_table(
            "opportunity_keys",
            sa.Column(
                "opportunity_id",
                sa.String(length=36),
                sa.ForeignKey("opportunities.id"),
                primary_key=True,
            ),
            sa.Column("kind", sa.String(length=20), primary_key=True),
            sa.Column("value", sa.String(length=998), primary_key=True),
        )
    if "ix_opportunity_keys_lookup" not in _indexes("opportunity_keys"):
        op.create_index("ix_opportunity_keys_lookup", "opportunity_keys", ["kind", "value"])

    if "merge_log" not in tables:
        json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
        op.create_table(
            "merge_log",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("kind", sa.String(length=10), nullable=False),
            sa.Column("survivor_id", sa.String(length=36), nullable=False),
            sa.Column("other_id", sa.String(length=36), nullable=False),
            sa.Column("actor_user_id", sa.String(length=36), sa.ForeignKey("users.id")),
            sa.Column("reason", sa.Text()),
            sa.Column("before", json_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("undone_at", sa.DateTime(timezone=True)),
            sa.Column("undone_by_user_id", sa.String(length=36), sa.ForeignKey("users.id")),
        )
        op.create_index("ix_merge_log_survivor_id", "merge_log", ["survivor_id"])
        op.create_index("ix_merge_log_other_id", "merge_log", ["other_id"])

    existing = _columns("opportunities")
    if "geohash" not in existing:
        op.add_column(
            "opportunities",
            sa.Column("geohash", sa.String(length=12), nullable=False, server_default=""),
        )
    if "ix_opportunities_geohash" not in _indexes("opportunities"):
        op.create_index("ix_opportunities_geohash", "opportunities", ["geohash"])
    if "material_change" not in existing:
        op.add_column(
            "opportunities",
            sa.Column("material_change", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "copies" not in _columns("addenda"):
        op.add_column(
            "addenda", sa.Column("copies", sa.Integer(), nullable=False, server_default="1")
        )
    if "source_message_id" not in _columns("outcomes"):
        op.add_column(
            "outcomes",
            sa.Column(
                "source_message_id",
                sa.String(length=36),
                sa.ForeignKey("raw_messages.id"),
                nullable=True,
            ),
        )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # `env.py` runs every migration inside one transaction, so a failed statement aborts all of it
    # and a bare try/except would leave the connection unusable ("current transaction is aborted").
    # The savepoint keeps the failure local, which is what makes the fallback real: a role without
    # rights to `CREATE EXTENSION` is a supported deployment, and candidate generation degrades to
    # token search on its own (see `pipeline._has_pg_trgm`).
    try:
        with bind.begin_nested():
            op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as e:  # noqa: BLE001 - an unprivileged role is a supported deployment
        log.warning("pg_trgm unavailable (%s); candidate generation falls back to token search", e)
        return
    try:
        with bind.begin_nested():
            if "ix_opportunities_normalized_name_trgm" not in _indexes("opportunities"):
                op.execute(
                    "CREATE INDEX ix_opportunities_normalized_name_trgm ON opportunities "
                    "USING gin (normalized_name gin_trgm_ops)"
                )
    except Exception as e:  # noqa: BLE001
        log.warning("could not create the trigram index (%s); token search still works", e)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_opportunities_normalized_name_trgm")
    if "source_message_id" in _columns("outcomes"):
        op.drop_column("outcomes", "source_message_id")
    if "copies" in _columns("addenda"):
        op.drop_column("addenda", "copies")
    existing = _columns("opportunities")
    if "ix_opportunities_geohash" in _indexes("opportunities"):
        op.drop_index("ix_opportunities_geohash", table_name="opportunities")
    if "material_change" in existing:
        op.drop_column("opportunities", "material_change")
    if "geohash" in existing:
        op.drop_column("opportunities", "geohash")
    tables = _tables()
    if "merge_log" in tables:
        op.drop_table("merge_log")
    if "opportunity_keys" in tables:
        op.drop_table("opportunity_keys")
