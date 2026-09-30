"""Deterministic fit scoring (SPEC-04, ADR-005)."""

from bidtriage.scoring.engine import ScoreResult, score
from bidtriage.scoring.profile import DEFAULT_PROFILE, Profile
from bidtriage.scoring.snapshot import CalendarSnapshot, GCSnapshot, OpportunitySnapshot

__all__ = [
    "score",
    "ScoreResult",
    "Profile",
    "DEFAULT_PROFILE",
    "OpportunitySnapshot",
    "GCSnapshot",
    "CalendarSnapshot",
]
