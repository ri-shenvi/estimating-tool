"""Inputs to the scoring function. Assembled by the caller; the engine does no I/O."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OpportunitySnapshot(BaseModel):
    opportunity_id: str = ""
    project_type: str = "unknown"
    trade_relevance: str = "primary"
    stated_electrical_value: float | None = None
    stated_project_value: float | None = None
    square_feet: float | None = None
    distance_miles: float | None = None
    bid_due: datetime | None = None
    prebid_at: datetime | None = None
    prebid_mandatory: bool | None = None
    bid_type: str = "unknown"
    sector: str = "unknown"
    flags: list[str] = Field(default_factory=list)
    scope_items: list[str] = Field(default_factory=list)
    owner_name: str | None = None
    status: str = "new"
    gc_is_owner_direct: bool = False


class GCSnapshot(BaseModel):
    gc_id: str | None = None
    name: str | None = None
    tier: str = "unknown"
    submitted_12m: int = 0
    hit_rate_12m: float | None = None
    key_account: bool = False


class CalendarSnapshot(BaseModel):
    now: datetime
    other_bids_due_same_week: int = 0
