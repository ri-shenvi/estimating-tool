"""Extraction eval harness (SPEC-02 "Technical Notes").

Every fixture in `tests/fixtures/messages` is an `.eml` plus the `.expected.json` an estimator
would have written. Two different jobs run over that corpus, and keeping them apart matters:

`evaluate()` measures **accuracy** — it calls a real extractor and compares field by field against
the expectations, with the SPEC-02 goals as gates: due date 97%, GC name 98%, project type 90%,
size band 80%, and the ITB / not-ITB boundary 97%. It only means anything against the model.

`validate_corpus()` measures **the corpus itself** — every fixture is schema-valid, post-processes
without error, and obeys the invariants SPEC-02 F2 states (summary under 60 words, source excerpts
under 200 characters, source locations drawn from the stated vocabulary, dates that resolve). No
model, no network, and no accuracy claim: comparing the fixture-backed extractor against the
fixtures would compare a file to itself and report 100% however broken the post-processor is.
Regressions in post-processing are caught by `tests/test_extraction_fixtures.py`, which asserts
concrete values, and by the scheduled online eval.

Expectations are post-processed before comparison, so the fixture files stay in the model's own
vocabulary (`10-16`, `weekday:friday@12:00`) and the date rules under test are production's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from bidtriage.extraction.postprocess import location_label, postprocess
from bidtriage.extraction.protocol import ExtractionInput, Extractor
from bidtriage.extraction.schema import (
    SOURCE_EXCERPT_CHARS,
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


# ------------------------------------------------------------------ corpus validation


@dataclass
class CorpusProblem:
    name: str
    detail: str


@dataclass
class CorpusReport:
    """What the offline pass can honestly establish about the fixture corpus."""

    checked: int = 0
    problems: list[CorpusProblem] = field(default_factory=list)
    kinds: dict[str, int] = field(default_factory=dict)
    with_due_date: int = 0
    with_attachment_sources: int = 0
    flags: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


_SOURCED = ("project_name", "project_number", "gc_name", "owner_name", "architect_engineer")


_ALL_SOURCED = (
    *_SOURCED,
    "location",
    "size_signals",
    "bid_due",
    "prebid",
    "rfi_deadline",
    "intent_due",
)


def _field_problems(expectation: LLMExtraction, record: ExtractedOpportunity) -> list[str]:
    """The F2 invariants a fixture must satisfy whatever the model would have said.

    Checked against the expectation as written, not the post-processed record: the post-processor
    truncates an over-long excerpt and drops an invalid location, so reading the record back would
    only confirm that the post-processor works. The point here is whether the *label* is right.
    """
    out: list[str] = []
    for label in _ALL_SOURCED:
        stated = getattr(expectation, label)
        source = getattr(stated, "source", None)
        if source is not None and len(source) > SOURCE_EXCERPT_CHARS:
            out.append(
                f"{label}.source is {len(source)} chars, over the {SOURCE_EXCERPT_CHARS} cap"
            )
        location = getattr(stated, "source_location", None)
        if location is not None and location_label(location) != location:
            out.append(f"{label}.source_location {location!r} is outside the F2 vocabulary")
    stated_words = len(expectation.summary.split())
    if stated_words > 60:
        out.append(f"summary is {stated_words} words, over the 60-word cap")
    if not record.summary:
        out.append("summary is empty after post-processing")
    if not 0.0 <= record.kind_confidence <= 1.0:
        out.append(f"kind_confidence {record.kind_confidence} is outside 0..1")
    return out


def validate_corpus(cases: Iterable[EvalCase]) -> CorpusReport:
    """Post-process every fixture and check it against the F2 invariants. No model, no network."""
    report = CorpusReport()
    for case in cases:
        report.checked += 1
        try:
            record = postprocess(case.expected, case.item.sent_at)
        except Exception as e:  # noqa: BLE001 - a fixture that cannot post-process is the finding
            report.problems.append(CorpusProblem(case.name, f"{type(e).__name__}: {e}"))
            continue
        for detail in _field_problems(case.expected, record):
            report.problems.append(CorpusProblem(case.name, detail))
        report.kinds[record.kind.value] = report.kinds.get(record.kind.value, 0) + 1
        if record.bid_due.value is not None:
            report.with_due_date += 1
        locations = [
            getattr(getattr(record, f), "source_location", None)
            for f in (*_SOURCED, "location", "bid_due")
        ]
        if any(loc and loc.startswith("attachment:") for loc in locations):
            report.with_attachment_sources += 1
        for flag in record.flags:
            report.flags[flag.value] = report.flags.get(flag.value, 0) + 1
        # A stated date that does not resolve is a broken fixture, not a low-accuracy one.
        for label in ("bid_due", "rfi_deadline", "intent_due"):
            stated = getattr(case.expected, label).value
            resolved = getattr(record, label).value
            if stated and resolved is None:
                report.problems.append(
                    CorpusProblem(case.name, f"{label} {stated!r} did not resolve to a datetime")
                )
    return report


def format_corpus_report(report: CorpusReport) -> Sequence[str]:
    lines = [f"{report.checked} fixture(s) validated against the SPEC-02 F2 invariants"]
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(report.kinds.items()))
    lines.append(f"  kinds: {kinds}")
    lines.append(f"  with a resolved bid due date: {report.with_due_date}")
    lines.append(f"  sourcing a field to an attachment: {report.with_attachment_sources}")
    if report.flags:
        lines.append("  flags: " + ", ".join(f"{k} {n}" for k, n in sorted(report.flags.items())))
    for p in report.problems:
        lines.append(f"  PROBLEM {p.name}: {p.detail}")
    lines.append("")
    lines.append(
        "Accuracy is not measured here: the fixture-backed extractor returns the fixtures. "
        "Run `make eval-extraction` against the model for the SPEC-02 gates."
    )
    return lines
