"""Resolve an extracted GC name / contact domains to a directory record (SPEC-07 F2). Pure."""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from bidtriage.extraction.postprocess import PLATFORM_DOMAINS
from bidtriage.resolution.normalize import normalize_domain

GENERIC_DOMAINS = {
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "aol.com",
    "icloud.com",
    "comcast.net",
    "verizon.net",
    "live.com",
    "msn.com",
}
_SUFFIXES = (
    " inc",
    " incorporated",
    " llc",
    " lp",
    " l p",
    " co",
    " company",
    " corp",
    " corporation",
    " construction",
    " builders",
    " contractors",
    " group",
)


@dataclass
class GCRecord:
    id: str
    canonical_name: str
    aliases: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    tier: str = "unknown"


@dataclass
class GCMatch:
    gc: GCRecord | None
    method: str  # domain | alias | fuzzy | none
    confidence: float


def _squash(name: str) -> str:
    """Collapse initials: 'P.J. Dick' -> 'pj dick'."""
    return " ".join("".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace()).split())


def _norm(name: str) -> str:
    s = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in name.lower())
    s = " ".join(s.split())
    return s


def _core(name: str) -> str:
    s = " " + _norm(name) + " "
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf + " "):
                s = s[: -len(suf) - 1] + " "
                changed = True
    return s.strip()


def resolve_gc(
    name: str | None,
    contact_domains: list[str],
    records: list[GCRecord],
    *,
    fuzzy_threshold: float = 0.9,
) -> GCMatch:
    domains = {normalize_domain(d) for d in contact_domains if d}
    domains = {
        d
        for d in domains
        if d
        and d not in GENERIC_DOMAINS
        and not any(d == p or d.endswith("." + p) for p in PLATFORM_DOMAINS)
    }
    for d in domains:
        for r in records:
            if d in r.domains:
                return GCMatch(r, "domain", 0.98)
    if name:
        n = _norm(name)
        core = _core(name)
        sq = _squash(name)
        for r in records:
            cands = {r.canonical_name, *r.aliases}
            names = {_norm(x) for x in cands} | {_squash(x) for x in cands}
            if n in names or sq in names:
                return GCMatch(r, "alias", 0.95)
        best: tuple[float, GCRecord | None] = (0.0, None)
        for r in records:
            for cand in {r.canonical_name, *r.aliases}:
                s = max(fuzz.ratio(n, _norm(cand)), fuzz.ratio(core, _core(cand))) / 100.0
                if s > best[0]:
                    best = (s, r)
        if best[1] is not None and best[0] >= fuzzy_threshold:
            return GCMatch(best[1], "fuzzy", round(best[0], 3))
    return GCMatch(None, "none", 0.0)
