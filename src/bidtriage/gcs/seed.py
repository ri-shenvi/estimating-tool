from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.models import GC, GCAlias, GCDomain

SEED_PATH = Path(__file__).with_name("data") / "gcs_seed.csv"


class SeedRow(TypedDict):
    canonical_name: str
    kind: str
    domains: list[str]
    aliases: list[str]


def load_seed_rows(path: Path = SEED_PATH) -> list[SeedRow]:
    with path.open(encoding="utf-8") as f:
        rows: list[SeedRow] = []
        for r in csv.DictReader(f):
            rows.append(
                SeedRow(
                    canonical_name=r["canonical_name"].strip(),
                    kind=r["kind"].strip() or "gc",
                    domains=[d.strip().lower() for d in r["domains"].split(";") if d.strip()],
                    aliases=[a.strip() for a in r["aliases"].split(";") if a.strip()],
                )
            )
        return rows


def seed_gcs(session: Session, path: Path = SEED_PATH) -> int:
    """Idempotent: creates missing GCs, adds missing aliases/domains, never overwrites tiers."""
    created = 0
    now = datetime.now(tz=UTC)
    for row in load_seed_rows(path):
        gc = session.scalar(select(GC).where(GC.canonical_name == row["canonical_name"]))
        if gc is None:
            gc = GC(
                canonical_name=row["canonical_name"],
                kind=row["kind"],
                created_from="seed",
                created_at=now,
            )
            session.add(gc)
            session.flush()
            created += 1
        existing_aliases = {a.alias for a in gc.aliases}
        existing_domains = {d.domain for d in gc.domains}
        for a in row["aliases"]:
            if a not in existing_aliases:
                session.add(GCAlias(gc_id=gc.id, alias=a))
        for d in row["domains"]:
            if d not in existing_domains:
                session.add(GCDomain(gc_id=gc.id, domain=d))
    session.flush()
    return created
