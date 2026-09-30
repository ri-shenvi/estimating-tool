"""Extraction through the pipeline on SQLite: geocoding, GC canonicalization, failure, retry, re-extraction."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from bidtriage.core.clock import BUSINESS_TZ, FrozenClock
from bidtriage.core.jobs import JobFailedError, claim
from bidtriage.core.models import Extraction, GeocodeCache, Job, Source
from bidtriage.extraction.fake import FakeExtractor
from bidtriage.extraction.geocode import CachedGeocoder, GeoResult, apply_geocode, geocode_queries
from bidtriage.extraction.protocol import ExtractionInput, ExtractionRefusedError
from bidtriage.extraction.schema import GeoLocation, GeoPrecision, Kind
from bidtriage.gcs.seed import seed_gcs
from bidtriage.ingestion.eml import parse_eml
from bidtriage.worker import handlers, main, pipeline

NOW = datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ)
HOME = (40.3462, -79.9482)


_DOWNTOWN = GeoResult(40.4406, -79.9959)


class StubGeocoder:
    """Answers with the same point for any query, and counts how often it was asked."""

    def __init__(self, result: GeoResult | None = _DOWNTOWN) -> None:
        self.result = result
        self.queries: list[str] = []

    def __call__(self, query: str) -> GeoResult | None:
        self.queries.append(query)
        return self.result


class FailingExtractor:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def extract(self, item: ExtractionInput):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise self.error


def _ingest(session, fixtures_dir, clock, name):  # type: ignore[no-untyped-def]
    src = session.scalar(select(Source))
    if src is None:
        src = Source(kind="file", name="fx", backfill_done=True, paused=True)
        session.add(src)
        session.flush()
    msg, _ = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id=name,
        parsed=parse_eml((fixtures_dir / name).read_bytes()),
        clock=clock,
    )
    return msg


@contextmanager
def _scope(session):  # type: ignore[no-untyped-def]
    yield session
    session.commit()


def _run_once(monkeypatch, session, ctx):  # type: ignore[no-untyped-def]
    """Run the real job loop against the test session, so rollback semantics are the real ones."""
    monkeypatch.setattr(main, "session_scope", lambda: _scope(session))
    return main.run_once(ctx)


# ------------------------------------------------------------------ geocoding


def test_geocode_queries_are_ordered_most_precise_first():  # type: ignore[no-untyped-def]
    loc = GeoLocation(
        raw="100 Terminal Way, Pittsburgh, PA 15219",
        street="100 Terminal Way",
        city="Pittsburgh",
        state="PA",
        postal_code="15219",
    )
    queries = geocode_queries(loc)
    assert queries[0] == ("100 Terminal Way, Pittsburgh, PA 15219", GeoPrecision.exact)
    assert queries[1] == ("Pittsburgh, PA 15219", GeoPrecision.city)


def test_vague_location_geocodes_to_the_city_centroid():  # type: ignore[no-untyped-def]
    loc = GeoLocation(raw="downtown Pittsburgh", city="Pittsburgh", state="PA")
    out = apply_geocode(loc, StubGeocoder())
    assert out.geo == (40.4406, -79.9959) and out.geo_precision == GeoPrecision.city
    assert out.raw == "downtown Pittsburgh"


def test_geocode_failure_leaves_geo_null():  # type: ignore[no-untyped-def]
    def boom(query: str) -> GeoResult | None:
        raise TimeoutError("geocoder down")

    out = apply_geocode(GeoLocation(city="Pittsburgh", state="PA"), boom)
    assert out.geo is None and out.geo_precision == GeoPrecision.none


def test_geocoder_miss_leaves_geo_null():  # type: ignore[no-untyped-def]
    out = apply_geocode(GeoLocation(city="Nowhere"), StubGeocoder(result=None))
    assert out.geo is None


def test_location_with_nothing_to_geocode_makes_no_calls():  # type: ignore[no-untyped-def]
    stub = StubGeocoder()
    assert apply_geocode(GeoLocation(), stub).geo is None and stub.queries == []


def test_geocode_cache_asks_the_provider_once(session):  # type: ignore[no-untyped-def]
    stub = StubGeocoder()
    cached = CachedGeocoder(session, stub)
    first = cached("Pittsburgh, PA")
    second = cached("  pittsburgh,   PA ")
    assert first == second and stub.queries == ["Pittsburgh, PA"]
    assert (
        session.scalar(select(GeocodeCache).where(GeocodeCache.query == "Pittsburgh, PA"))
        is not None
    )


def test_extraction_geocodes_and_resolution_reuses_the_point(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    seed_gcs(session)
    msg = _ingest(session, fixtures_dir, clock, "wv_location.eml")
    stub = StubGeocoder(GeoResult(39.6556, -79.9553, GeoPrecision.exact))
    ext = pipeline.extract_message(
        session,
        msg,
        FakeExtractor(fixtures_dir),
        external_ref="wv_location",
        clock=clock,
        geocoder=CachedGeocoder(session, stub),
    )
    assert ext is not None
    assert ext.payload["location"]["geo_precision"] == "exact"
    assert ext.payload["location"]["state"] == "WV"
    opp, _ = pipeline.resolve_message(session, msg, ext, clock=clock)
    # Morgantown is in the service area: geocoded, kept, and close enough to score.
    assert (round(opp.lat, 3), round(opp.lon, 3)) == (39.656, -79.955)
    res = pipeline.score_opportunity(session, opp, now=clock.now(), home=HOME)
    assert "location" not in res.missing_inputs


# ------------------------------------------------------------------ GC canonicalization


def test_gc_name_comes_from_the_domain_when_the_signature_is_an_image(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    seed_gcs(session)
    msg = _ingest(session, fixtures_dir, clock, "gc_from_domain.eml")
    ext = pipeline.extract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="gc_from_domain", clock=clock
    )
    # The extraction record stays faithful to the message: the model saw no company name.
    assert ext is not None and ext.payload["gc_name"]["value"] is None
    opp, _ = pipeline.resolve_message(session, msg, ext, clock=clock)
    assert opp.canonical["gc_name"]["value"] == "Mascaro Construction"
    assert opp.canonical["gc_name"]["confidence"] >= 0.9
    from bidtriage.core.models import GC

    assert session.get(GC, opp.gc_id).canonical_name == "Mascaro Construction"


def test_platform_sender_is_not_used_as_the_gc(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    seed_gcs(session)
    msg = _ingest(session, fixtures_dir, clock, "procore_table.eml")
    ext = pipeline.extract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="procore_table", clock=clock
    )
    assert ext is not None
    opp, _ = pipeline.resolve_message(session, msg, ext, clock=clock)
    assert opp.canonical["gc_name"]["value"] == "Continental Building Company"
    emails = [c["email"] for c in opp.canonical["gc_contacts"] if c["email"]]
    assert not any("procore.com" in e for e in emails)
    assert opp.canonical["gc_domain"] != "procore.com"


# ------------------------------------------------------------------ pre-filter


def test_vendor_newsletter_is_classified_without_an_llm_call(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "vendor_newsletter.eml")

    class Explode:
        def extract(self, item):  # type: ignore[no-untyped-def]
            raise AssertionError("the pre-filter should have skipped this message")

    assert pipeline.extract_message(session, msg, Explode(), clock=clock) is None
    assert msg.kind == Kind.not_bid.value and msg.extraction_status == "skipped"
    assert session.scalars(select(Extraction)).all() == []


# ------------------------------------------------------------------ failure, retry, recovery


def test_three_failures_mark_the_message_and_keep_it_visible(session, fixtures_dir, monkeypatch):  # type: ignore[no-untyped-def]
    """SPEC-02 F3: retry three times, then `extraction_failed` and into Needs review."""
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "refusal_stub.eml")
    extractor = FailingExtractor(ConnectionError("anthropic unreachable"))
    ctx = handlers.Context(extractor, geocoder=StubGeocoder())
    # Ingestion already queued the extraction; retry that job rather than adding a second one, or
    # the loop would alternate between two jobs and neither would reach its third attempt.
    job = session.scalar(select(Job).where(Job.kind == "extract_message"))
    assert job is not None and job.max_attempts == 3

    statuses = []
    for _ in range(3):
        job.run_at = datetime.now(tz=UTC) - timedelta(seconds=1)  # skip the backoff wait
        session.flush()
        assert _run_once(monkeypatch, session, ctx)
        statuses.append(msg.extraction_status)

    assert statuses == ["retrying", "retrying", "failed"]
    assert extractor.calls == 3
    assert msg.extraction_attempts == 3
    assert "ConnectionError" in msg.extraction_error
    assert job.status == "failed" and job.attempts == 3

    from bidtriage.worker.digest_job import review_items

    kinds = [r.kind for r in review_items(session, "http://x")]
    assert "extraction_failed" in kinds
    assert msg in pipeline.messages_needing_review(session)


def test_a_failed_message_is_retried_when_the_api_recovers(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "refusal_stub.eml")
    msg.extraction_status, msg.extraction_attempts = "failed", 3
    msg.extraction_error = "error: ConnectionError: anthropic unreachable"
    session.flush()

    assert pipeline.enqueue_extraction_retries(session, clock=clock) == 1
    job = session.scalar(select(Job).where(Job.key.like(f"extract:{msg.id}:retry:%")))
    assert job is not None and job.priority == pipeline.RETRY_PRIORITY

    # Same hour bucket: the sweep is idempotent and does not pile jobs up.
    assert pipeline.enqueue_extraction_retries(session, clock=clock) == 0

    ctx = handlers.Context(FakeExtractor(fixtures_dir), geocoder=StubGeocoder())
    claim(session, clock=clock)
    handlers.extract_message(session, {"message_id": msg.id}, ctx)
    assert msg.extraction_status == "done" and msg.extraction_error is None
    assert msg.kind == Kind.itb.value


def test_a_refusal_is_retried_three_times_then_left_alone(session, fixtures_dir):  # type: ignore[no-untyped-def]
    """A refusal retries like any failure, but the hourly sweep leaves it: it is not an outage."""
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "refusal_stub.eml")
    extractor = FailingExtractor(ExtractionRefusedError("refused: policy"))
    for attempt in (1, 2, 3):
        with pytest.raises(ExtractionRefusedError):
            pipeline.extract_message(
                session, msg, extractor, clock=clock, attempt=attempt, max_attempts=3
            )
    assert msg.extraction_status == "failed" and msg.extraction_attempts == 3
    assert msg.extraction_error.startswith("refusal:")
    assert pipeline.enqueue_extraction_retries(session, clock=clock) == 0
    assert msg in pipeline.messages_needing_review(session)


def test_the_handler_turns_a_failure_into_a_retriable_job_error(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "refusal_stub.eml")
    ctx = handlers.Context(FailingExtractor(ExtractionRefusedError("refused: policy")))
    with pytest.raises(JobFailedError):
        handlers.extract_message(session, {"message_id": msg.id}, ctx)
    assert msg.extraction_status == "retrying" and msg.extraction_attempts == 1


def test_the_retry_sweep_gives_up_eventually(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "refusal_stub.eml")
    msg.extraction_status, msg.extraction_attempts = "failed", 24
    msg.extraction_error = "error: ConnectionError: still down"
    session.flush()
    assert pipeline.enqueue_extraction_retries(session, clock=clock, max_rounds=24) == 0
    # It stops costing money, but it never stops being visible.
    assert msg in pipeline.messages_needing_review(session)


def test_low_confidence_kind_is_processed_and_flagged_for_review(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "ocr_garbled.eml")
    ext = pipeline.extract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="ocr_garbled", clock=clock
    )
    assert ext is not None and msg.kind == Kind.itb.value and msg.extraction_status == "done"
    assert msg in pipeline.messages_needing_review(session)
    from bidtriage.worker.digest_job import review_items

    assert "low_confidence_kind" in [r.kind for r in review_items(session, "http://x")]
    # Still resolved and scored like anything else: low confidence is not a dead end.
    opp, decision = pipeline.resolve_message(session, msg, ext, clock=clock)
    assert decision == "new" and opp is not None


# ------------------------------------------------------------------ re-extraction


def test_reextraction_supersedes_the_previous_record(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    seed_gcs(session)
    msg = _ingest(session, fixtures_dir, clock, "gc_email_benedum.eml")
    first = pipeline.extract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="gc_email_benedum", clock=clock
    )
    second = pipeline.reextract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="gc_email_benedum", clock=clock
    )
    assert first is not None and second is not None
    assert (first.version, second.version) == (1, 2)
    assert first.superseded_by == second.id and second.superseded_by is None
    assert pipeline.latest_extraction(session, msg.id).id == second.id
    # The old payload is retained, not overwritten.
    assert first.payload["project_name"]["value"] == "Benedum Hall Lab Renovation"
    resolve_keys = {
        j.key for j in session.scalars(select(Job)).all() if j.kind == "resolve_message"
    }
    assert f"resolve:{msg.id}:{second.id}" in resolve_keys


def test_reextraction_ignores_the_prefilter(session, fixtures_dir):  # type: ignore[no-untyped-def]
    """An estimator asking for a second look overrides the cheap skip."""
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "vendor_newsletter.eml")
    assert pipeline.extract_message(session, msg, FakeExtractor(fixtures_dir), clock=clock) is None
    ext = pipeline.reextract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="vendor_newsletter", clock=clock
    )
    assert ext is not None and msg.extraction_status == "done"


def test_prompt_bump_reextracts_only_recent_messages(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    recent = _ingest(session, fixtures_dir, clock, "gc_email_benedum.eml")
    old = _ingest(session, fixtures_dir, clock, "roofing_itb.eml")
    old.received_at = NOW - timedelta(days=45)
    for msg, stem in ((recent, "gc_email_benedum"), (old, "roofing_itb")):
        pipeline.extract_message(
            session, msg, FakeExtractor(fixtures_dir), external_ref=stem, clock=clock
        )
        ext = pipeline.latest_extraction(session, msg.id)
        ext.prompt_version = "v0"
    session.flush()

    assert (
        pipeline.enqueue_stale_prompt_reextractions(session, clock=clock, prompt_version="v9") == 1
    )
    keys = {
        j.key for j in session.scalars(select(Job).where(Job.kind == "reextract_message")).all()
    }
    assert keys == {f"reextract:{recent.id}:v9"}
    # Already on the current version: nothing to do.
    assert (
        pipeline.enqueue_stale_prompt_reextractions(session, clock=clock, prompt_version="v0") == 0
    )


def test_reextract_handler_runs_the_extractor(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "roofing_itb.eml")
    ctx = handlers.Context(FakeExtractor(fixtures_dir), geocoder=StubGeocoder())
    handlers.reextract_message(session, {"message_id": msg.id}, ctx)
    ext = pipeline.latest_extraction(session, msg.id)
    assert ext is not None and ext.payload["location"]["lat"] is not None


def test_handlers_ignore_a_message_that_vanished(session, fixtures_dir):  # type: ignore[no-untyped-def]
    ctx = handlers.Context(FakeExtractor(fixtures_dir))
    handlers.extract_message(session, {"message_id": "gone"}, ctx)
    handlers.reextract_message(session, {"message_id": "gone"}, ctx)


def test_scheduled_tick_queues_the_sweeps(session):  # type: ignore[no-untyped-def]
    ctx = handlers.Context(None)
    handlers.schedule_tick(session, ctx, now=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    kinds = {j.kind for j in session.scalars(select(Job)).all()}
    assert {"retry_extractions", "reextract_stale"} <= kinds
    stale = session.scalar(select(Job).where(Job.kind == "reextract_stale"))
    assert stale.priority == pipeline.REEXTRACT_PRIORITY


def test_sweep_handlers_are_registered_and_runnable(session, fixtures_dir):  # type: ignore[no-untyped-def]
    ctx = handlers.Context(FakeExtractor(fixtures_dir))
    assert {"retry_extractions", "reextract_stale", "reextract_message"} <= set(handlers.HANDLERS)
    handlers.HANDLERS["retry_extractions"](session, {}, ctx)
    handlers.HANDLERS["reextract_stale"](session, {}, ctx)


def test_extractions_are_not_reextracted_while_extraction_is_pending(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "roofing_itb.eml")
    assert msg.extraction_status == "pending"
    assert (
        pipeline.enqueue_stale_prompt_reextractions(session, clock=clock, prompt_version="v9") == 0
    )


def test_message_needing_review_query_excludes_healthy_messages(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "gc_email_benedum.eml")
    pipeline.extract_message(
        session, msg, FakeExtractor(fixtures_dir), external_ref="gc_email_benedum", clock=clock
    )
    assert pipeline.messages_needing_review(session) == []


def test_attempts_accumulate_across_retry_rounds(session, fixtures_dir):  # type: ignore[no-untyped-def]
    """The sweep's give-up cap counts every try ever made, not just the current job's."""
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "refusal_stub.eml")
    extractor = FailingExtractor(ConnectionError("still down"))
    for _round in range(2):
        for attempt in (1, 2, 3):
            with pytest.raises(ConnectionError):
                pipeline.extract_message(
                    session, msg, extractor, clock=clock, attempt=attempt, max_attempts=3
                )
    assert msg.extraction_attempts == 6 and msg.extraction_status == "failed"
    assert pipeline.enqueue_extraction_retries(session, clock=clock, max_rounds=6) == 0
    assert pipeline.enqueue_extraction_retries(session, clock=clock, max_rounds=9) == 1


def test_vague_location_fixture_geocodes_to_a_city_centroid(session, fixtures_dir):  # type: ignore[no-untyped-def]
    """ "downtown Pittsburgh" keeps its raw phrase and lands on the city, not on a street."""
    clock = FrozenClock(NOW)
    seed_gcs(session)
    msg = _ingest(session, fixtures_dir, clock, "vague_location.eml")
    stub = StubGeocoder(GeoResult(40.4406, -79.9959))
    ext = pipeline.extract_message(
        session,
        msg,
        FakeExtractor(fixtures_dir),
        external_ref="vague_location",
        clock=clock,
        geocoder=CachedGeocoder(session, stub),
    )
    assert ext is not None
    location = ext.payload["location"]
    assert location["raw"] == "downtown Pittsburgh"
    assert location["geo_precision"] == "city"
    # No street was stated, so no street-level query was ever attempted.
    assert stub.queries == ["Pittsburgh, PA"]
    opp, _ = pipeline.resolve_message(session, msg, ext, clock=clock)
    assert (round(opp.lat, 3), round(opp.lon, 3)) == (40.441, -79.996)


def test_prefilter_skip_clears_a_stale_failure_reason(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg = _ingest(session, fixtures_dir, clock, "vendor_newsletter.eml")
    msg.extraction_status, msg.extraction_attempts = "failed", 3
    msg.extraction_error = "error: ConnectionError: anthropic unreachable"
    session.flush()
    assert pipeline.extract_message(session, msg, FakeExtractor(fixtures_dir), clock=clock) is None
    assert msg.extraction_status == "skipped" and msg.extraction_error is None
    assert msg not in pipeline.messages_needing_review(session)
