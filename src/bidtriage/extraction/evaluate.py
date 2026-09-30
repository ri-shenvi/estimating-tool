"""Extraction eval harness (SPEC-02 "Technical Notes").

Every fixture in `tests/fixtures/messages` is an `.eml` plus the `.expected.json` an estimator
would have written. This runs the extractor over each one and reports per-field accuracy against
the goals in SPEC-02: due date 97%, GC name 98%, project type 90%, size band 80%, and the
ITB / not-ITB classification boundary 97%. CI fails when a gate slips.

Expectations are themselves post-processed before comparison, so the fixture files stay in the
model's own vocabulary (`10-16`, `weekday:friday@12:00`) and the date rules under test are the same
ones production applies.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from bidtriage.extraction.postprocess import postprocess
from bidtriage.extraction.protocol import ExtractionInput, Extractor
from bidtriage.extraction.schema import (
    ExtractedOpportunity,
    Kind,
    LLMExtraction,
    SizeSignals,
)

# Coarse buckets for "did we read the size roughly right" — not SPEC-04's scoring band.
_VALUE_BANDS = (
    (250_000, "<=250K"),
    (1_000_000, "250K-1M"),
    (4_000_000, "1M-4M"),
    (15_000_000, "4M-15M"),
)

# Per-field gates from the SPEC-02 goals. Fields absent here are reported but never fail CI.
THRESHOLDS: dict[str, float] = {
    "bid_due": 0.97,
    "gc_name": 0.98,
    "project_type": 0.90,
    "size_band": 0.80,
    "itb_boundary": 0.97,
}


def _band(value: float | None) -> str:
    if not value:
        return "none"
    for ceiling, label in _VALUE_BANDS:
        if value <= ceiling:
            return label
    return ">15M"


def size_band(size: SizeSignals) -> str:
    """One label per message, so a mis-read $12M vs $1.2M counts as a miss."""
    if size.stated_electrical_value:
        return f"elec:{_band(size.stated_electrical_value)}"
    if size.stated_project_value:
        return f"proj:{_band(size.stated_project_value)}"
    return "unknown"


def _due_key(record: ExtractedOpportunity) -> str | None:
    due = record.bid_due
    if due.value is None:
        return None
    return due.value.isoformat() if due.time_known else due.value.date().isoformat()


def _text(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def compare(expected: ExtractedOpportunity, got: ExtractedOpportunity) -> dict[str, bool]:
    """Field-by-field verdicts for one message."""
    return {
        "kind": got.kind == expected.kind,
        # The boundary SPEC-02 holds to 97%: is this a new invitation to bid, or is it not?
        "itb_boundary": (got.kind == Kind.itb) == (expected.kind == Kind.itb),
        "bid_due": _due_key(got) == _due_key(expected),
        "prebid": (got.prebid.value.isoformat() if got.prebid.value else None)
        == (expected.prebid.value.isoformat() if expected.prebid.value else None),
        "gc_name": _text(got.gc_name.value) == _text(expected.gc_name.value),
        "owner_name": _text(got.owner_name.value) == _text(expected.owner_name.value),
        "project_type": got.project_type == expected.project_type,
        "size_band": size_band(got.size_signals) == size_band(expected.size_signals),
        "sector": got.sector == expected.sector,
        "bid_type": got.bid_type == expected.bid_type,
        "trade_relevance": got.trade_relevance == expected.trade_relevance,
        "city": _text(got.location.city) == _text(expected.location.city),
        "flags": {f.value for f in got.flags} == {f.value for f in expected.flags},
        "scope_items": {s.value for s in got.scope_items}
        == {s.value for s in expected.scope_items},
    }


@dataclass
class EvalCase:
    name: str
    item: ExtractionInput
    expected: LLMExtraction


@dataclass
class CaseResult:
    name: str
    got: ExtractedOpportunity
    expected: ExtractedOpportunity
    verdicts: dict[str, bool]
    error: str | None = None

    @property
    def misses(self) -> list[str]:
        return [f for f, ok in self.verdicts.items() if not ok]


@dataclass
class FieldScore:
    name: str
    correct: int
    total: int

    @property
    def ratio(self) -> float:
        return self.correct / self.total if self.total else 1.0

    @property
    def threshold(self) -> float | None:
        return THRESHOLDS.get(self.name)

    @property
    def passed(self) -> bool:
        return self.threshold is None or self.ratio >= self.threshold


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def fields(self) -> list[FieldScore]:
        names = [f for c in self.cases for f in c.verdicts]
        ordered = list(dict.fromkeys(names))
        return [
            FieldScore(n, sum(1 for c in self.cases if c.verdicts.get(n)), len(self.cases))
            for n in ordered
        ]

    @property
    def gate_failures(self) -> list[FieldScore]:
        return [f for f in self.fields if not f.passed]


def evaluate(cases: Iterable[EvalCase], extractor: Extractor) -> EvalReport:
    report = EvalReport()
    for case in cases:
        expected = postprocess(case.expected, case.item.sent_at)
        try:
            got = extractor.extract(case.item)
        except Exception as e:  # noqa: BLE001 - a failing message is a result, not a crash
            report.errors.append((case.name, f"{type(e).__name__}: {e}"))
            continue
        report.cases.append(CaseResult(case.name, got, expected, compare(expected, got)))
    return report


def format_report(report: EvalReport, *, cases: bool = True) -> Sequence[str]:
    lines: list[str] = []
    if cases:
        for c in report.cases:
            misses = ", ".join(c.misses) or "all fields match"
            lines.append(
                f"{c.name}: kind={c.got.kind.value} due={_due_key(c.got)} "
                f"gc={c.got.gc_name.value} type={c.got.project_type.value} — {misses}"
            )
    for name, err in report.errors:
        lines.append(f"{name}: ERROR {err}")
    lines.append("")
    lines.append(f"{len(report.cases)} fixture(s) evaluated, {len(report.errors)} error(s)")
    for f in report.fields:
        gate = f" (gate {f.threshold:.0%}: {'PASS' if f.passed else 'FAIL'})" if f.threshold else ""
        lines.append(f"  {f.name:<16} {f.correct}/{f.total} {f.ratio:.0%}{gate}")
    return lines
