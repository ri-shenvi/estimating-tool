"""Scoring profile schema and defaults (SPEC-04 F1-F5, F7). Edited by the chief estimator; versioned."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class Weights(BaseModel):
    project_type: float = 0.25
    size: float = 0.25
    gc: float = 0.25
    distance: float = 0.10
    timing: float = 0.10
    bid_type: float = 0.05

    def total(self) -> float:
        return self.project_type + self.size + self.gc + self.distance + self.timing + self.bid_type


class SizeBand(BaseModel):
    min_floor: float = 75_000
    sweet_low: float = 250_000
    sweet_high: float = 4_000_000
    max_ceiling: float = 8_000_000
    hard_max: float = 15_000_000
    ceiling_value: float = 0.2
    unknown_value: float = 0.5

    @model_validator(mode="after")
    def _ordered(self) -> SizeBand:
        seq = [self.min_floor, self.sweet_low, self.sweet_high, self.max_ceiling, self.hard_max]
        if any(a >= b for a, b in zip(seq, seq[1:], strict=False)):
            raise ValueError("size band breakpoints must be strictly increasing")
        return self


class DistanceCurve(BaseModel):
    near_miles: float = 25
    near_value: float = 1.0
    mid_miles: float = 50
    mid_value: float = 0.7
    far_miles: float = 90
    far_value: float = 0.3
    beyond_value: float = 0.1
    unknown_value: float = 0.6


class TimingTable(BaseModel):
    under_3_days: float = 0.2
    days_3_to_6: float = 0.6
    days_7_to_21: float = 1.0
    days_22_to_45: float = 0.9
    over_45: float = 0.7
    unknown: float = 0.5
    congestion_2: float = 0.8
    congestion_3_plus: float = 0.6


class Thresholds(BaseModel):
    bid: int = 70
    consider: int = 45
    likely_pass: int = 20


class Boosts(BaseModel):
    renewables: int = 5
    key_account: int = 8
    design_build_or_bim: int = 3
    sustainability: int = 2
    requested_by_name: int = 5


class Caps(BaseModel):
    no_electrical_scope: int = 5
    open_shop: int = 20
    mandatory_prebid_passed: int = 10
    due_passed: int = 5
    above_hard_max: int = 15
    residential: int = 5


DEFAULT_TYPE_TABLE: dict[str, float] = {
    "higher_education": 1.0,
    "healthcare": 0.95,
    "commercial_office": 0.9,
    "high_tech_research_data_center": 0.9,
    "religious": 0.85,
    "light_industrial_utility": 0.8,
    "multifamily_hotel_mixed_use": 0.6,
    "k12_education": 0.6,
    "government_civic": 0.6,
    "retail_restaurant": 0.5,
    "parking_transportation": 0.4,
    "heavy_industrial": 0.2,
    "site_civil_only": 0.1,
    "residential_single_family": 0.0,
    "other": 0.4,
    "unknown": 0.5,
}

DEFAULT_ELECTRICAL_SHARE: dict[str, float] = {
    "high_tech_research_data_center": 0.22,
    "healthcare": 0.15,
    "higher_education": 0.13,
    "commercial_office": 0.11,
    "k12_education": 0.11,
    "religious": 0.10,
    "light_industrial_utility": 0.10,
    "heavy_industrial": 0.12,
    "multifamily_hotel_mixed_use": 0.08,
    "retail_restaurant": 0.08,
    "government_civic": 0.11,
    "parking_transportation": 0.07,
    "site_civil_only": 0.05,
    "residential_single_family": 0.06,
    "other": 0.10,
    "unknown": 0.10,
}

DEFAULT_COST_PER_SF: dict[str, float] = {
    "high_tech_research_data_center": 900,
    "healthcare": 600,
    "higher_education": 500,
    "commercial_office": 350,
    "k12_education": 400,
    "religious": 350,
    "light_industrial_utility": 200,
    "heavy_industrial": 300,
    "multifamily_hotel_mixed_use": 300,
    "retail_restaurant": 250,
    "government_civic": 450,
    "parking_transportation": 80,
    "site_civil_only": 50,
    "residential_single_family": 200,
    "other": 300,
    "unknown": 300,
}

DEFAULT_GC_TIER: dict[str, float] = {
    "A": 1.0,
    "B": 0.8,
    "C": 0.5,
    "D": 0.2,
    "blocked": 0.0,
    "unknown": 0.5,
}

DEFAULT_BID_TYPE: dict[str, float] = {
    "negotiated": 1.0,
    "design_assist": 1.0,
    "design_build": 0.9,
    "gmp": 0.7,
    "budget": 0.7,
    "hard_bid_private": 0.7,
    "hard_bid_public": 0.6,
    "unknown": 0.7,
}


class Profile(BaseModel):
    name: str = "default"
    weights: Weights = Field(default_factory=Weights)
    type_table: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_TYPE_TABLE))
    partial_relevance_multiplier: float = 0.6
    electrical_share: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_ELECTRICAL_SHARE)
    )
    cost_per_sf: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_COST_PER_SF))
    size_band: SizeBand = Field(default_factory=SizeBand)
    gc_tier: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_GC_TIER))
    owner_direct_default: float = 0.6
    hit_rate_min_sample: int = 5
    hit_rate_high: float = 0.25
    hit_rate_low: float = 0.05
    hit_rate_adjust: float = 0.1
    distance: DistanceCurve = Field(default_factory=DistanceCurve)
    timing: TimingTable = Field(default_factory=TimingTable)
    bid_type: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_BID_TYPE))
    thresholds: Thresholds = Field(default_factory=Thresholds)
    boosts: Boosts = Field(default_factory=Boosts)
    caps: Caps = Field(default_factory=Caps)
    key_accounts: list[str] = Field(default_factory=list)
    #: Below this, an extracted date is "low confidence" for the `past_due_at_receipt` reason.
    low_confidence: float = 0.6

    @model_validator(mode="after")
    def _validate(self) -> Profile:
        if abs(self.weights.total() - 1.0) > 0.001:
            raise ValueError(f"weights must sum to 1.0 (got {self.weights.total():.3f})")
        for w, v in self.weights.model_dump().items():
            if not 0 <= v <= 1:
                raise ValueError(f"weights.{w} must be within 0..1")
        for table in ("type_table", "electrical_share", "gc_tier", "bid_type"):
            for k, v in getattr(self, table).items():
                if not 0 <= v <= 1:
                    raise ValueError(f"{table}[{k}] must be within 0..1")
        for name, v in (
            ("partial_relevance_multiplier", self.partial_relevance_multiplier),
            ("owner_direct_default", self.owner_direct_default),
            ("hit_rate_high", self.hit_rate_high),
            ("hit_rate_low", self.hit_rate_low),
            ("hit_rate_adjust", self.hit_rate_adjust),
            ("low_confidence", self.low_confidence),
            *(
                (f"distance.{k}", v)
                for k, v in self.distance.model_dump().items()
                if "miles" not in k
            ),
            *((f"timing.{k}", v) for k, v in self.timing.model_dump().items()),
            ("size_band.ceiling_value", self.size_band.ceiling_value),
            ("size_band.unknown_value", self.size_band.unknown_value),
        ):
            if not 0 <= v <= 1:
                raise ValueError(f"{name} must be within 0..1")
        for k, v in self.cost_per_sf.items():
            if v <= 0:
                raise ValueError(f"cost_per_sf[{k}] must be positive")
        if not any(abs(v - 1.0) < 1e-9 for v in self.type_table.values()):
            raise ValueError("at least one project type must have factor 1.0")
        if not (self.thresholds.likely_pass < self.thresholds.consider < self.thresholds.bid):
            raise ValueError("thresholds must be ordered likely_pass < consider < bid")
        if self.thresholds.likely_pass < 0 or self.thresholds.bid > 100:
            raise ValueError("thresholds must be within 0..100")
        if self.hit_rate_low > self.hit_rate_high:
            raise ValueError("hit_rate_low must not exceed hit_rate_high")
        for name, v in (
            ("renewables", self.boosts.renewables),
            ("key_account", self.boosts.key_account),
            ("design_build_or_bim", self.boosts.design_build_or_bim),
            ("sustainability", self.boosts.sustainability),
            ("requested_by_name", self.boosts.requested_by_name),
        ):
            if not 0 <= v <= 100:
                raise ValueError(f"boosts.{name} must be within 0..100")
        for name, v in self.caps.model_dump().items():
            if not 0 <= v <= 100:
                raise ValueError(f"caps.{name} must be within 0..100")
        return self

    def validation_warnings(self) -> list[str]:
        """Non-fatal problems worth showing in the editor (SPEC-04 F7, `test_profile_missing_type`).

        A project type the table does not mention still scores — it falls back to `other` — so a
        missing row is a warning, not a rejection. Saying so is the point: silently scoring
        healthcare as "other" is exactly the kind of surprise this profile exists to prevent.
        """
        out: list[str] = []
        fallback = self.type_table.get("other", 0.4)
        for t in sorted(set(DEFAULT_TYPE_TABLE) - set(self.type_table)):
            out.append(
                f"project type '{t}' is missing from the type table; "
                f"it will fall back to other ({fallback:g})"
            )
        for t in sorted(set(DEFAULT_ELECTRICAL_SHARE) - set(self.electrical_share)):
            out.append(f"project type '{t}' has no electrical share; it will fall back to unknown")
        if "other" not in self.type_table:
            out.append("the type table has no 'other' row; unlisted types will score 0.4")
        return out

    def neutral(self, factor: str) -> float:
        """What a factor is worth when nothing is known about it (SPEC-04 F6).

        This is the line between "a reason to bid" and "an absence of information". It is derived
        from the profile rather than hard-coded, so an estimator who decides an unknown GC is worth
        0.4 moves the bar for what counts as a positive along with it.
        """
        return {
            "project_type": self.type_table.get("unknown", 0.5),
            "size": self.size_band.unknown_value,
            "gc": self.gc_tier.get("unknown", 0.5),
            "distance": self.distance.unknown_value,
            "timing": self.timing.unknown,
            "bid_type": self.bid_type.get("unknown", 0.7),
        }[factor]


DEFAULT_PROFILE = Profile()
