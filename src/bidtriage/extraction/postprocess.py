"""Deterministic post-processing of LLM output (SPEC-02 F3). The model never has the last word on a date."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.extraction.schema import (
    DateTimeField,
    ExtractedOpportunity,
    ExtractionMeta,
    Flag,
    LLMDateTimeField,
    LLMExtraction,
    LLMPrebid,
    Prebid,
    StrField,
)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NO_YEAR = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[T ](\d{2}):(\d{2}))?$")


def _zone(name: str | None) -> ZoneInfo:
    if not name:
        return BUSINESS_TZ
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return BUSINESS_TZ


def parse_local(
    value: str | None, tz_name: str | None, anchor: datetime
) -> tuple[datetime | None, bool]:
    """Parse an ISO-ish local datetime string into an aware datetime. Returns (dt, time_known)."""
    if not value:
        return None, False
    value = value.strip()
    tz = _zone(tz_name)
    m = _NO_YEAR.match(value)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = anchor.astimezone(tz).year
        try:
            candidate = datetime(year, month, day, tzinfo=tz)
        except ValueError:
            return None, False
        if candidate.date() < anchor.astimezone(tz).date() - timedelta(days=2):
            candidate = candidate.replace(year=year + 1)
        if m.group(3):
            candidate = candidate.replace(hour=int(m.group(3)), minute=int(m.group(4)))
            return candidate, True
        return candidate, False
    if _DATE_ONLY.match(value):
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=tz), False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, True


def _dt_field(f: LLMDateTimeField, anchor: datetime) -> DateTimeField:
    dt, time_known = parse_local(f.value, f.timezone, anchor)
    return DateTimeField(
        value=dt,
        time_known=time_known and f.time_known or time_known,
        confidence=f.confidence if dt else 0.0,
        source=f.source,
    )


def _prebid(p: LLMPrebid, anchor: datetime) -> Prebid:
    dt, _ = parse_local(p.value, p.timezone, anchor)
    return Prebid(
        value=dt,
        location=p.location,
        mandatory=p.mandatory,
        confidence=p.confidence if dt else 0.0,
        source=p.source,
    )


def _clean_str(f: StrField) -> StrField:
    if f.value is not None:
        v = " ".join(f.value.split())
        return StrField(value=v or None, confidence=f.confidence if v else 0.0, source=f.source)
    return f


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
    if bid_due.value is not None and bid_due.value < sent_at - timedelta(days=2):
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

    contacts = [c for c in x.gc_contacts if not (c.email and _is_platform_email(c.email))]

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
        gc_name=_clean_str(x.gc_name),
        gc_contacts=contacts,
        owner_name=_clean_str(x.owner_name),
        architect_engineer=_clean_str(x.architect_engineer),
        location=x.location,
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
        size_signals=x.size_signals,
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


def _is_platform_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in PLATFORM_DOMAINS)
