"""Opportunity resolution: dedupe, threading, change tracking (SPEC-03, ADR-008)."""

from bidtriage.resolution.evidence import Candidate, Evidence, Incoming, decide, evidence
from bidtriage.resolution.merge import DateMergeOutcome, merge_date
from bidtriage.resolution.normalize import fingerprint, normalize_name

__all__ = [
    "normalize_name",
    "fingerprint",
    "Candidate",
    "Incoming",
    "Evidence",
    "evidence",
    "decide",
    "merge_date",
    "DateMergeOutcome",
]
