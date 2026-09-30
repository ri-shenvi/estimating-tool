"""Deterministic post-processing of LLM output (SPEC-02 F3). The model never has the last word on a date.

Structured outputs guarantee a schema-valid record; they guarantee nothing about truthfulness. So
every rule an estimator would be hurt by getting wrong — the year of a bare "January 8", a due date
already in the past, a platform address masquerading as the GC, a missing summary — is enforced
here, in pure code, over whatever the model returned.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.extraction.schema import (
    SOURCE_EXCERPT_CHARS,
    Contact,
    DateTimeField,
    ExtractedOpportunity,
    ExtractionMeta,
    Flag,
    GeoLocation,
    LLMDateTimeField,
    LLMExtraction,
    LLMPrebid,
    Location,
    Prebid,
    SizeSignals,
    StrField,
)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NO_YEAR = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?$")
_WEEKDAY = re.compile(r"^weekday:([a-z]{3})[a-z]*(?:@(\d{1,2}):(\d{2}))?$", re.I)
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# How stale a year-less date may be before it is read as "just missed" rather than "next year".
# Ties the F3 year rule ("first occurrence on or after the sent date") to the F3 past-due rule:
# inside the window the date stays in the past and is flagged; outside it rolls forward.
STALE_GRACE = timedelta(days=2)

_LOCATION_OK = re.compile(r"^(subject|body|attachment:)", re.I)


def _zone(name: str | None) -> ZoneInfo:
    if not name:
        return BUSINESS_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return BUSINESS_TZ


def resolve_weekday(
    name: str, anchor: datetime, at: time | None = None, tz: ZoneInfo | None = None
) -> datetime | None:
    """First `name` on or after `anchor`'s local date. 'Bids due Friday at noon' (SPEC-02 F3)."""
    target = _WEEKDAYS.get(name[:3].lower())
    if target is None:
        return None
    tz = tz or BUSINESS_TZ
    local = anchor.astimezone(tz)
    ahead = (target - local.weekday()) % 7
    day = local.date() + timedelta(days=ahead)
    return datetime.combine(day, at or time(0, 0), tzinfo=tz)


def parse_local(
    value: str | None, tz_name: str | None, anchor: datetime
) -> tuple[datetime | None, bool]:
    """Parse the model's date string into an aware datetime. Returns (dt, time_known).

    Accepts, in the order the prompt offers them: a full ISO local datetime, an ISO date, a
    year-less `MM-DD`/`M/D` (year chosen from the anchor) and `weekday:friday@12:00`.
    """
    if not value:
        return None, False
    value = value.strip()
    tz = _zone(tz_name)

    wd = _WEEKDAY.match(value)
    if wd:
        at = time(int(wd.group(2)) % 24, int(wd.group(3))) if wd.group(2) else None
        dt = resolve_weekday(wd.group(1), anchor, at, tz)
        return dt, bool(dt and at)

    m = _NO_YEAR.match(value)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        local = anchor.astimezone(tz)
        try:
            candidate = datetime(local.year, month, day, tzinfo=tz)
        except ValueError:
            return None, False
        if candidate.date() < local.date() - STALE_GRACE:
            try:
                candidate = candidate.replace(year=local.year + 1)
            except ValueError:  # Feb 29 in a non-leap year
                return None, False
        if m.group(3):
            candidate = candidate.replace(hour=int(m.group(3)) % 24, minute=int(m.group(4)))
            return candidate, True
        return candidate, False

    if _DATE_ONLY.match(value):
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return None, False
        return datetime(d.year, d.month, d.day, tzinfo=tz), False

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, True


def _excerpt(source: str | None) -> str | None:
    """Sources are verbatim quotes capped at 200 characters (SPEC-02 F2)."""
    if not source:
        return None
    text = " ".join(source.split())
    if not text:
        return None
    if len(text) > SOURCE_EXCERPT_CHARS:
        text = text[: SOURCE_EXCERPT_CHARS - 1].rstrip() + "…"
    return text


def _location_label(value: str | None) -> str | None:
    """`subject`, `body` or `attachment:<name>[:p<page>]`; anything else is not a location."""
    if not value:
        return None
    text = " ".join(value.split())
    if text.lower() in ("subject", "body"):
        return text.lower()
    return text if _LOCATION_OK.match(text) else None


def _time_known(field: LLMDateTimeField, dt: datetime | None, parsed_time: bool) -> bool:
    """A midnight the model did not claim as a time is not a time (SPEC-02 F2)."""
    if dt is None or not parsed_time:
        return False
    if dt.hour == 0 and dt.minute == 0 and not field.time_known:
        return False
    return True


def _dt_field(f: LLMDateTimeField, anchor: datetime) -> DateTimeField:
    dt, parsed_time = parse_local(f.value, f.timezone, anchor)
    return DateTimeField(
        value=dt,
        time_known=_time_known(f, dt, parsed_time),
        timezone=str(dt.tzinfo) if dt is not None else None,
        confidence=f.confidence if dt else 0.0,
        source=_excerpt(f.source),
        source_location=_location_label(f.source_location),
    )


def _prebid(p: LLMPrebid, anchor: datetime) -> Prebid:
    dt, _ = parse_local(p.value, p.timezone, anchor)
    return Prebid(
        value=dt,
        location=p.location,
        mandatory=p.mandatory,
        confidence=p.confidence if dt else 0.0,
        source=_excerpt(p.source),
        source_location=_location_label(p.source_location),
    )


def _clean_str(f: StrField) -> StrField:
    value = " ".join(f.value.split()) or None if f.value is not None else None
    return StrField(
        value=value,
        confidence=f.confidence if value else 0.0,
        source=_excerpt(f.source),
        source_location=_location_label(f.source_location),
    )


def _geo_location(loc: Location) -> GeoLocation:
    out = GeoLocation.model_validate(loc.model_dump())
    out.source = _excerpt(loc.source)
    out.source_location = _location_label(loc.source_location)
    return out


def _size(size: SizeSignals) -> SizeSignals:
    out = size.model_copy()
    out.source = _excerpt(size.source)
    out.source_location = _location_label(size.source_location)
    return out


def synthesize_summary(x: LLMExtraction) -> str:
    bits = []
    if x.project_name.value:
        bits.append(x.project_name.value)
    if x.project_type.value != "unknown":
        bits.append(x.project_type.value.replace("_", " "))
    if x.scope_items:
        bits.append("scope: " + ", ".join(s.value.replace("_", " ") for s in x.scope_items[:6]))
    if x.gc_name.value:
        bits.append(f"from {x.gc_name.value}")
    return "; ".join(bits) or "No summary available."


def postprocess(
    x: LLMExtraction, sent_at: datetime, meta: ExtractionMeta | None = None
) -> ExtractedOpportunity:
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    flags = list(dict.fromkeys(x.flags))

    bid_due = _dt_field(x.bid_due, sent_at)
    if bid_due.value is not None and bid_due.value < sent_at - STALE_GRACE:
        bid_due.confidence = min(bid_due.confidence, 0.3)
        if Flag.past_due_at_receipt not in flags:
            flags.append(Flag.past_due_at_receipt)

    prebid = _prebid(x.prebid, sent_at)
    if prebid.mandatory and Flag.mandatory_prebid not in flags:
        flags.append(Flag.mandatory_prebid)

    summary = " ".join(x.summary.split())
    if not summary:
        summary = synthesize_summary(x)
        flags.append(Flag.summary_synthesized)
    words = summary.split()
    if len(words) > 60:
        summary = " ".join(words[:60]) + "…"

    contacts = _clean_contacts(x.gc_contacts)

    start: date | None = None
    if x.anticipated_start:
        try:
            start = date.fromisoformat(x.anticipated_start[:10])
        except ValueError:
            start = None

    return ExtractedOpportunity(
        kind=x.kind,
        kind_confidence=x.kind_confidence,
        project_name=_clean_str(x.project_name),
        project_number=_clean_str(x.project_number),
        gc_name=_gc_name(x.gc_name),
        gc_contacts=contacts,
        owner_name=_clean_str(x.owner_name),
        architect_engineer=_clean_str(x.architect_engineer),
        location=_geo_location(x.location),
        project_type=x.project_type,
        project_type_confidence=x.project_type_confidence,
        project_subtype=x.project_subtype,
        new_or_renovation=x.new_or_renovation,
        bid_type=x.bid_type,
        sector=x.sector,
        delivery_channel=x.delivery_channel,
        bid_due=bid_due,
        prebid=prebid,
        rfi_deadline=_dt_field(x.rfi_deadline, sent_at),
        intent_due=_dt_field(x.intent_due, sent_at),
        anticipated_start=start,
        duration_months=x.duration_months,
        size_signals=_size(x.size_signals),
        scope_items=list(dict.fromkeys(x.scope_items)),
        scope_text=x.scope_text,
        exclusions_text=x.exclusions_text,
        flags=flags,
        document_links=x.document_links,
        addendum_label=x.addendum_label,
        addendum_number=x.addendum_number,
        changes_described=x.changes_described,
        trade_relevance=x.trade_relevance,
        summary=summary,
        extraction_meta=meta or ExtractionMeta(),
    )


PLATFORM_DOMAINS = {
    "buildingconnected.com",
    "procore.com",
    "procoretech.com",
    "isqft.com",
    "constructconnect.com",
    "planhub.com",
    "smartbidnet.com",
    "smartbid.co",
    "panteratools.com",
    "construction.com",
    "downtobid.com",
    "bidmail.com",
}

# Bid-invitation platforms are never the general contractor (SPEC-02 F3). The model is told this,
# but a platform digest whose only visible company is the platform itself tempts it anyway.
PLATFORM_BRANDS = {
    "buildingconnected",
    "building connected",
    "procore",
    "isqft",
    "constructconnect",
    "planhub",
    "smartbid",
    "smartbidnet",
    "pantera",
    "pantera tools",
    "dodge",
    "dodge construction network",
    "downtobid",
    "bid mail",
    "bidmail",
}


def is_platform_name(name: str | None) -> bool:
    if not name:
        return False
    squashed = "".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace()).strip()
    return squashed in PLATFORM_BRANDS


def _gc_name(f: StrField) -> StrField:
    cleaned = _clean_str(f)
    if is_platform_name(cleaned.value):
        return StrField(
            value=None,
            confidence=0.0,
            source=cleaned.source,
            source_location=cleaned.source_location,
        )
    return cleaned


def _clean_contacts(contacts: list[Contact]) -> list[Contact]:
    out: list[Contact] = []
    for c in contacts:
        if c.email and _is_platform_email(c.email):
            continue
        if c.email is None and c.phone is None and is_platform_name(c.name):
            continue
        out.append(c)
    return out


def _is_platform_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in PLATFORM_DOMAINS)
