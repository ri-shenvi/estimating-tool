"""Calibration arithmetic for the labeled corpus (SPEC-04 technical notes, ADR-005).

Pure: the caller reads the labels and the stored scores, this turns them into the numbers the
chief estimator argues with. Keeping it out of the CLI is what makes the thresholds testable
without a database behind them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: SPEC-04 goals for the "bid" band on the labeled corpus.
PRECISION_GOAL = 0.80
RECALL_GOAL = 0.85

BANDS = ("bid", "consider", "likely_pass", "pass")


@dataclass(frozen=True)
class LabeledScore:
    """One opportunity the chief estimator labelled, next to what the engine said about it."""

    opportunity_id: str
    label: str
    band: str
    score: int
    project_type: str = "unknown"
    gc_tier: str = "unknown"

    @property
    def truth_bid(self) -> bool:
        return self.label.strip().lower() in ("bid", "1", "true", "yes")

    @property
    def predicted_bid(self) -> bool:
        return self.band == "bid"


@dataclass(frozen=True)
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


def confusion(rows: Iterable[LabeledScore]) -> Counts:
    tp = fp = fn = tn = 0
    for r in rows:
        if r.predicted_bid and r.truth_bid:
            tp += 1
        elif r.predicted_bid:
            fp += 1
        elif r.truth_bid:
            fn += 1
        else:
            tn += 1
    return Counts(tp, fp, fn, tn)


def by_group(rows: Iterable[LabeledScore], key: Callable[[LabeledScore], str]) -> dict[str, Counts]:
    buckets: dict[str, list[LabeledScore]] = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)
    return {k: confusion(v) for k, v in sorted(buckets.items())}


def band_breakdown(rows: Iterable[LabeledScore]) -> dict[str, Counts]:
    """Per predicted band, how the estimator's own labels fell.

    Read `tp` as "labelled bid" and `fp` as "labelled pass" for bands other than Bid: outside the
    bid band the engine is not claiming anything, so precision/recall are only meaningful for Bid.
    """
    out: dict[str, Counts] = {}
    for band in BANDS:
        in_band = [r for r in rows if r.band == band]
        bids = sum(1 for r in in_band if r.truth_bid)
        out[band] = Counts(tp=bids, fp=len(in_band) - bids)
    return out


def _pct(v: float | None) -> str:
    return "  n/a" if v is None else f"{v * 100:5.1f}%"


def _table(title: str, groups: dict[str, Counts]) -> list[str]:
    lines = [
        title,
        f"  {'group':<34} {'n':>4} {'tp':>4} {'fp':>4} {'fn':>4} {'prec':>6} {'rec':>6}",
    ]
    for name, c in groups.items():
        lines.append(
            f"  {name:<34} {c.n:>4} {c.tp:>4} {c.fp:>4} {c.fn:>4} "
            f"{_pct(c.precision)} {_pct(c.recall)}"
        )
    return lines


def report(
    rows: list[LabeledScore],
    *,
    precision_goal: float = PRECISION_GOAL,
    recall_goal: float = RECALL_GOAL,
) -> tuple[str, bool]:
    """The printable report and whether it clears the SPEC-04 goals."""
    if not rows:
        return ("No labelled opportunity has a score yet; nothing to calibrate.", False)
    overall = confusion(rows)
    lines = [
        f"Labelled opportunities scored: {len(rows)}",
        "",
        f"Bid band: precision {_pct(overall.precision)} (goal {precision_goal:.0%}) · "
        f"recall {_pct(overall.recall)} (goal {recall_goal:.0%})",
        f"  tp={overall.tp} fp={overall.fp} fn={overall.fn} tn={overall.tn}",
        "",
        "Where the labels landed, by predicted band",
        f"  {'band':<34} {'n':>4} {'labelled bid':>13} {'labelled pass':>14}",
    ]
    for band, c in band_breakdown(rows).items():
        lines.append(f"  {band:<34} {c.tp + c.fp:>4} {c.tp:>13} {c.fp:>14}")
    lines += ["", *_table("By project type", by_group(rows, lambda r: r.project_type))]
    lines += ["", *_table("By GC tier", by_group(rows, lambda r: f"tier {r.gc_tier}"))]
    passed = (overall.precision or 0) >= precision_goal and (overall.recall or 0) >= recall_goal
    lines += [
        "",
        "PASS: the profile clears both goals."
        if passed
        else "BELOW GOAL: tune the profile (SPEC-04) before trusting the band.",
    ]
    return ("\n".join(lines), passed)
