from datetime import datetime, timedelta

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.gcs.resolve import GCRecord, resolve_gc
from bidtriage.gcs.seed import load_seed_rows, seed_gcs
from bidtriage.gcs.stats import OppSummary, compute_stats

RECORDS = [
    GCRecord("1", "PJ Dick", {"P.J. Dick", "PJ Dick Incorporated"}, {"pjdick.com", "pjdick.net"}),
    GCRecord("2", "Continental Building Company", {"Continental"}, {"continentalbuilding.com"}),
    GCRecord("3", "Continental Construction", set(), {"continentalconstruction.com"}),
    GCRecord("4", "Mascaro Construction", {"Mascaro"}, {"mascaroconstruction.com"}),
]


def test_domain_match():  # type: ignore[no-untyped-def]
    m = resolve_gc("Some Name", ["jsmith@pjdick.net"], RECORDS)
    assert m.gc.id == "1" and m.method == "domain"


def test_alias_match_with_generic_email():  # type: ignore[no-untyped-def]
    m = resolve_gc("P.J. Dick Incorporated", ["someone@gmail.com"], RECORDS)
    assert m.gc.id == "1" and m.method == "alias"


def test_fuzzy_match():  # type: ignore[no-untyped-def]
    m = resolve_gc("Mascaro Construction Co LP", [], RECORDS)
    assert m.gc is not None and m.gc.id == "4"


def test_similar_names_distinct():  # type: ignore[no-untyped-def]
    m = resolve_gc("Continental Construction", [], RECORDS)
    assert m.gc.id == "3"


def test_platform_domain_ignored():  # type: ignore[no-untyped-def]
    m = resolve_gc(None, ["team@buildingconnected.com"], RECORDS)
    assert m.gc is None


def test_unknown_creates_nothing():  # type: ignore[no-untyped-def]
    m = resolve_gc("Keystone Builders", ["x@keystonebuilders.com"], RECORDS)
    assert m.gc is None and m.method == "none"


def test_seed_rows_and_idempotent(session):  # type: ignore[no-untyped-def]
    rows = load_seed_rows()
    assert any(r["canonical_name"] == "PJ Dick" for r in rows)
    assert seed_gcs(session) == len(rows)
    assert seed_gcs(session) == 0


def test_stats_min_sample():  # type: ignore[no-untyped-def]
    now = datetime(2026, 9, 30, tzinfo=BUSINESS_TZ)
    opps = [
        OppSummary(
            now - timedelta(days=30 * i), now - timedelta(days=30 * i - 9), True, True, i <= 2
        )
        for i in range(1, 7)
    ]
    s = compute_stats(opps, now=now)
    assert s.submitted == 6 and abs(s.hit_rate - 2 / 6) < 1e-9 and abs(s.avg_days_notice - 9) < 1e-9
    s4 = compute_stats(opps[:4], now=now)
    assert s4.hit_rate is None
    old = compute_stats([OppSummary(now - timedelta(days=366), None, True, True, True)], now=now)
    assert old.invites == 0
