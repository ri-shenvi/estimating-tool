"""Builders for SPEC-03 tests: a stored message plus its extraction, resolved in one call.

Messages are built here rather than read from `tests/fixtures/messages` so a resolution test can
state the one thing it is about — a reminder that disagrees, an addendum labelled "Bulletin 1" —
without adding a fixture to the extraction corpus, which measures something else entirely.
"""

from __future__ import annotations

import itertools
from datetime import datetime

from sqlalchemy import select

from bidtriage.core.clock import BUSINESS_TZ, Clock
from bidtriage.core.models import Extraction, MessageLink, Opportunity, RawMessage, Source
from bidtriage.extraction.postprocess import postprocess
from bidtriage.extraction.schema import ExtractionMeta, GeoPrecision, LLMExtraction
from bidtriage.worker import pipeline

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=BUSINESS_TZ)
DUE = "2026-10-16T14:00:00"
DUE_AWARE = datetime(2026, 10, 16, 14, 0, tzinfo=BUSINESS_TZ).isoformat()
PITT = {"raw": "3700 O'Hara St, Pittsburgh, PA", "city": "Pittsburgh", "state": "PA"}
PITT_LAT, PITT_LON = 40.4435, -79.9580

_serial = itertools.count()


def llm(**kw: object) -> LLMExtraction:
    """A plausible PJ Dick ITB, with any field overridden. Nested dicts merge rather than replace."""
    base: dict[str, object] = {
        "kind": "itb",
        "kind_confidence": 0.95,
        "project_name": {"value": "Benedum Hall Lab Renovation", "confidence": 0.95},
        "gc_name": {"value": "PJ Dick", "confidence": 0.95},
        "gc_contacts": [{"name": "Jane Doe", "email": "jdoe@pjdick.com"}],
        "location": dict(PITT, confidence=0.9),
        "project_type": "higher_education",
        "bid_due": {"value": DUE, "time_known": True, "confidence": 0.95},
        "scope_items": ["lighting"],
        "summary": "Electrical for a lab renovation.",
    }
    for key, value in kw.items():
        current = base.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            base[key] = {**current, **value}
        else:
            base[key] = value
    return LLMExtraction.model_validate(base)


def deliver(
    session,  # type: ignore[no-untyped-def]
    clock: Clock,
    *,
    subject: str = "Benedum Hall Lab Renovation - Electrical ITB",
    from_addr: str = "jdoe@pjdick.com",
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
    links: tuple[str, ...] = (),
    lat: float | None = PITT_LAT,
    lon: float | None = PITT_LON,
    **llm_kw: object,
) -> tuple[RawMessage, Opportunity, str]:
    """Store a message, its links and its extraction, then resolve it: one processing cycle.

    `links` are stored before resolution, the way SPEC-01 ingestion harvests them, so the platform
    hard keys in SPEC-03 F2.1 are visible to the matcher.
    """
    now, serial = clock.now(), next(_serial)
    src = session.scalar(select(Source)) or Source(kind="file", name="fx")
    session.add(src)
    msg = RawMessage(
        internet_message_id=message_id or f"<m{serial}@x>",
        content_hash=f"h{serial}",
        from_addr=from_addr,
        subject=subject,
        sent_at=now,
        received_at=now,
        body_text=subject,
        in_reply_to=in_reply_to,
        references=list(references),
        created_at=now,
    )
    session.add(msg)
    session.flush()
    for url in links:
        session.add(MessageLink(message_id=msg.id, url=url, host_class="other"))
    session.flush()
    result = postprocess(llm(**llm_kw), now, ExtractionMeta(model="test", prompt_version="test"))
    if lat is not None and result.location.raw:
        # SPEC-02 F3 geocodes at extraction time; these tests stand in for the geocoder.
        result.location.lat, result.location.lon = lat, lon
        result.location.geo_precision = GeoPrecision.street
    ext = Extraction(
        message_id=msg.id,
        version=1,
        model="test",
        prompt_version="test",
        payload=result.model_dump(mode="json"),
        created_at=now,
    )
    session.add(ext)
    session.flush()
    opp, decision = pipeline.resolve_message(session, msg, ext, clock=clock)
    return msg, opp, decision
