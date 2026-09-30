"""Extraction schema.

`LLMExtraction` is what the model returns (structured outputs; strings for dates so the JSON
schema stays simple and portable). `ExtractedOpportunity` is the post-processed domain record
with real datetimes, enforced date rules and derived flags. See SPEC-02 F2.

The schema is versioned with the prompt (`SCHEMA_VERSION` here, `prompts/extract_vN.md`): the two
are a pair, because a field the prompt never explains is a field the model fills badly.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Kind(StrEnum):
    itb = "itb"
    rfb = "rfb"
    addendum = "addendum"
    date_change = "date_change"
    reminder = "reminder"
    prebid_notice = "prebid_notice"
    rfi_response = "rfi_response"
    award = "award"
    platform_noise = "platform_noise"
    not_bid = "not_bid"


EXTRACTABLE_KINDS = {
    Kind.itb,
    Kind.rfb,
    Kind.addendum,
    Kind.date_change,
    Kind.reminder,
    Kind.prebid_notice,
    Kind.rfi_response,
    Kind.award,
}


class ProjectType(StrEnum):
    commercial_office = "commercial_office"
    healthcare = "healthcare"
    higher_education = "higher_education"
    k12_education = "k12_education"
    high_tech_research_data_center = "high_tech_research_data_center"
    light_industrial_utility = "light_industrial_utility"
    heavy_industrial = "heavy_industrial"
    multifamily_hotel_mixed_use = "multifamily_hotel_mixed_use"
    retail_restaurant = "retail_restaurant"
    religious = "religious"
    government_civic = "government_civic"
    parking_transportation = "parking_transportation"
    residential_single_family = "residential_single_family"
    site_civil_only = "site_civil_only"
    other = "other"
    unknown = "unknown"


class ScopeItem(StrEnum):
    service_and_distribution = "service_and_distribution"
    branch_power = "branch_power"
    lighting = "lighting"
    lighting_controls = "lighting_controls"
    site_lighting = "site_lighting"
    fire_alarm = "fire_alarm"
    structured_cabling = "structured_cabling"
    security_access_control = "security_access_control"
    av = "av"
    nurse_call = "nurse_call"
    generator_ats = "generator_ats"
    ups = "ups"
    solar_pv = "solar_pv"
    battery_storage = "battery_storage"
    ev_charging = "ev_charging"
    medium_voltage = "medium_voltage"
    temporary_power = "temporary_power"
    demolition = "demolition"
    bim_coordination = "bim_coordination"
    design_build_engineering = "design_build_engineering"
    controls_bms_interface = "controls_bms_interface"
    lightning_protection = "lightning_protection"
    other = "other"


class Flag(StrEnum):
    prevailing_wage = "prevailing_wage"
    davis_bacon = "davis_bacon"
    pla = "pla"
    union_required = "union_required"
    open_shop_indicated = "open_shop_indicated"
    bid_bond = "bid_bond"
    pp_bond = "pp_bond"
    liquidated_damages = "liquidated_damages"
    mbe_wbe_goals = "mbe_wbe_goals"
    mandatory_prebid = "mandatory_prebid"
    sealed_bid = "sealed_bid"
    plans_not_yet_available = "plans_not_yet_available"
    tax_exempt = "tax_exempt"
    phased = "phased"
    occupied_facility = "occupied_facility"
    night_work = "night_work"
    conflicting_dates = "conflicting_dates"
    past_due_at_receipt = "past_due_at_receipt"
    attachment_truncated = "attachment_truncated"
    summary_synthesized = "summary_synthesized"
    requested_by_name = "requested_by_name"
    sustainability = "sustainability"
    other = "other"


class BidType(StrEnum):
    hard_bid = "hard_bid"
    budget = "budget"
    gmp = "gmp"
    design_build = "design_build"
    design_assist = "design_assist"
    negotiated = "negotiated"
    unknown = "unknown"


class Sector(StrEnum):
    private = "private"
    public = "public"
    institutional_private = "institutional_private"
    federal = "federal"
    unknown = "unknown"


class Channel(StrEnum):
    buildingconnected = "buildingconnected"
    procore = "procore"
    isqft = "isqft"
    planhub = "planhub"
    smartbid = "smartbid"
    pantera = "pantera"
    dodge = "dodge"
    email = "email"
    public_notice = "public_notice"
    other = "other"


class NewOrRenovation(StrEnum):
    new = "new"
    renovation = "renovation"
    addition = "addition"
    tenant_fit_out = "tenant_fit_out"
    unknown = "unknown"


class TradeRelevance(StrEnum):
    primary = "primary"
    partial = "partial"
    none = "none"


class GeoPrecision(StrEnum):
    """How precisely `location` was geocoded. `none` means the geocoder found nothing."""

    exact = "exact"
    street = "street"
    city = "city"
    region = "region"
    none = "none"


SOURCE_EXCERPT_CHARS = 200
SCHEMA_VERSION = "v2"

# Below this, the classification is shown on the review page — but the message is still processed
# as its best guess, never dropped (SPEC-02 F1).
KIND_REVIEW_CONFIDENCE = 0.6


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Sourced(_Strict):
    """Every extracted field carries its own confidence and where it came from (SPEC-02 F2)."""

    confidence: float = Field(default=0.0, ge=0, le=1)
    source: str | None = Field(
        default=None, description="Verbatim excerpt from the message, <=200 chars"
    )
    source_location: str | None = Field(
        default=None,
        description="Where the excerpt is: 'subject', 'body', or 'attachment:<name>:p<page>'",
    )


class StrField(_Sourced):
    value: str | None = None


class LLMDateTimeField(_Sourced):
    value: str | None = Field(
        default=None,
        description=(
            "ISO 8601 local datetime, e.g. 2026-10-16T14:00:00; date-only allowed. "
            "'10-16' when the text states no year; 'weekday:friday@12:00' for a bare weekday."
        ),
    )
    timezone: str | None = Field(
        default=None, description="IANA zone if stated, e.g. America/Chicago"
    )
    time_known: bool = False


class LLMPrebid(_Sourced):
    value: str | None = None
    timezone: str | None = None
    location: str | None = None
    mandatory: bool | None = None


class Location(_Sourced):
    raw: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class SizeSignals(_Sourced):
    stated_project_value: float | None = None
    stated_electrical_value: float | None = None
    square_feet: float | None = None
    stories: int | None = None
    units_or_beds: int | None = None
    description: str | None = None


class Contact(_Strict):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str | None = None


class DocumentLink(_Strict):
    url: str
    host_class: str = "other"
    label: str | None = None


class LLMExtraction(_Strict):
    """What Claude returns. Keep JSON-schema-simple: enums, strings, numbers, booleans, lists."""

    kind: Kind
    kind_confidence: float = Field(ge=0, le=1)
    project_name: StrField = Field(default_factory=StrField)
    project_number: StrField = Field(default_factory=StrField)
    gc_name: StrField = Field(default_factory=StrField)
    gc_contacts: list[Contact] = Field(default_factory=list)
    owner_name: StrField = Field(default_factory=StrField)
    architect_engineer: StrField = Field(default_factory=StrField)
    location: Location = Field(default_factory=Location)
    project_type: ProjectType = ProjectType.unknown
    project_type_confidence: float = Field(default=0.0, ge=0, le=1)
    project_subtype: str | None = None
    new_or_renovation: NewOrRenovation = NewOrRenovation.unknown
    bid_type: BidType = BidType.unknown
    sector: Sector = Sector.unknown
    delivery_channel: Channel = Channel.email
    bid_due: LLMDateTimeField = Field(default_factory=LLMDateTimeField)
    prebid: LLMPrebid = Field(default_factory=LLMPrebid)
    rfi_deadline: LLMDateTimeField = Field(default_factory=LLMDateTimeField)
    intent_due: LLMDateTimeField = Field(default_factory=LLMDateTimeField)
    anticipated_start: str | None = Field(default=None, description="ISO date")
    duration_months: float | None = None
    size_signals: SizeSignals = Field(default_factory=SizeSignals)
    scope_items: list[ScopeItem] = Field(default_factory=list)
    scope_text: str | None = None
    exclusions_text: str | None = None
    flags: list[Flag] = Field(default_factory=list)
    document_links: list[DocumentLink] = Field(default_factory=list)
    addendum_label: str | None = None
    addendum_number: int | None = None
    changes_described: str | None = None
    trade_relevance: TradeRelevance = TradeRelevance.primary
    summary: str = ""


class DateTimeField(BaseModel):
    value: datetime | None = None
    time_known: bool = False
    timezone: str | None = None
    confidence: float = 0.0
    source: str | None = None
    source_location: str | None = None


class Prebid(BaseModel):
    value: datetime | None = None
    location: str | None = None
    mandatory: bool | None = None
    confidence: float = 0.0
    source: str | None = None
    source_location: str | None = None


class GeoLocation(Location):
    """`Location` after geocoding. The model never fills these; the post-processor does."""

    lat: float | None = None
    lon: float | None = None
    geo_precision: GeoPrecision = GeoPrecision.none

    @property
    def geo(self) -> tuple[float, float] | None:
        """None when the geocoder found nothing, per SPEC-02 F3."""
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)


class ExtractionMeta(BaseModel):
    model: str = "fake"
    prompt_version: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class ExtractedOpportunity(BaseModel):
    """Post-processed record used by resolution and scoring."""

    kind: Kind
    kind_confidence: float
    project_name: StrField
    project_number: StrField
    gc_name: StrField
    gc_contacts: list[Contact]
    owner_name: StrField
    architect_engineer: StrField
    location: GeoLocation
    project_type: ProjectType
    project_type_confidence: float
    project_subtype: str | None
    new_or_renovation: NewOrRenovation
    bid_type: BidType
    sector: Sector
    delivery_channel: Channel
    bid_due: DateTimeField
    prebid: Prebid
    rfi_deadline: DateTimeField
    intent_due: DateTimeField
    anticipated_start: date | None
    duration_months: float | None
    size_signals: SizeSignals
    scope_items: list[ScopeItem]
    scope_text: str | None
    exclusions_text: str | None
    flags: list[Flag]
    document_links: list[DocumentLink]
    addendum_label: str | None
    addendum_number: int | None
    changes_described: str | None
    trade_relevance: TradeRelevance
    summary: str
    extraction_meta: ExtractionMeta = Field(default_factory=ExtractionMeta)
