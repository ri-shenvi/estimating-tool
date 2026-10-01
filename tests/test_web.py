"""Web layer smoke tests with the SQLite session injected (SPEC-06 F2 pre-fetch safety, SPEC-09 health)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bidtriage.core.config import Settings, get_settings
from bidtriage.core.models import Opportunity, User
from bidtriage.decisions.tokens import sign_action
from bidtriage.web.app import create_app
from bidtriage.web.deps import db

SECRET = "web-test-secret-web-test-secret"


@pytest.fixture
def client(session):  # type: ignore[no-untyped-def]
    app = create_app()

    def _db():  # type: ignore[no-untyped-def]
        yield session
        session.flush()

    app.dependency_overrides[db] = _db
    app.dependency_overrides[get_settings] = lambda: Settings(
        SECRET_KEY=SECRET, BIDTRIAGE_ENV="dev"
    )
    return TestClient(app)


def _seed(session):  # type: ignore[no-untyped-def]
    u = User(id="u1", email="casey@example.com", name="Casey", role="chief")
    o = Opportunity(
        id="opp1",
        status="new",
        canonical={
            "project_name": {"value": "Benedum Hall"},
            "gc_name": {"value": "PJ Dick"},
            "bid_due": {"value": "2026-10-16T14:00:00-04:00", "time_known": True},
        },
        first_seen_at=datetime.now(tz=UTC),
        last_activity_at=datetime.now(tz=UTC),
    )
    session.add_all([u, o])
    session.flush()
    return u, o


def test_health(client):  # type: ignore[no-untyped-def]
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_index_and_detail(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    r = client.get("/")
    assert r.status_code == 200 and "Benedum Hall" in r.text
    r = client.get("/opportunities/opp1")
    assert r.status_code == 200 and "PJ Dick" in r.text
    assert client.get("/opportunities/nope").status_code == 404


def test_action_link_get_does_not_mutate_post_does(client, session):  # type: ignore[no-untyped-def]
    _, o = _seed(session)
    token = sign_action("opp1", "bid", "u1", SECRET)
    for _ in range(3):  # link scanners pre-fetch
        assert client.get(f"/a/{token}").status_code == 200
    session.refresh(o)
    assert o.status == "new"
    r = client.post(f"/a/{token}")
    assert r.status_code == 200 and "now <b>bidding</b>" in r.text
    session.refresh(o)
    assert o.status == "bidding" and o.assignee_user_id == "u1"
    r = client.post(f"/a/{token}")  # single use
    assert "already used" in r.text


def test_tampered_token_rejected(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    token = sign_action("opp1", "bid", "u1", SECRET)
    v, p, s = token.split(".")
    assert client.get(f"/a/{v}.{p}.{s[:-3]}abc").status_code == 400


def test_api_action_and_invalid_transition(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    r = client.post("/api/opportunities/opp1/actions", json={"action": "won"})
    assert r.status_code == 400 and "cannot move" in r.json()["detail"]
    r = client.post("/api/opportunities/opp1/actions", json={"action": "pass", "reason": "too_far"})
    assert r.status_code == 200 and r.json()["after"]["status"] == "passed"
    listing = client.get("/api/opportunities?status=passed").json()
    assert listing and listing[0]["id"] == "opp1"


def test_role_guard(client, session):  # type: ignore[no-untyped-def]
    session.add(User(id="u2", email="sam@example.com", name="Sam", role="estimator"))
    session.flush()
    r = client.post("/admin/profiles/1/activate", headers={"X-Dev-User": "sam@example.com"})
    assert r.status_code == 403


# ---------------------------------------------------------- SPEC-01: admin sources and upload


def test_admin_page_shows_source_health(client, session, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from bidtriage.core.models import Source

    _seed(session)
    session.add(
        Source(
            id="s1",
            kind="graph",
            name="Estimating mailbox",
            mailbox="estimating@ferryelectric.com",
            last_success_at=datetime.now(tz=UTC) - timedelta(minutes=90),
        )
    )
    session.flush()
    r = client.get("/admin/", headers={"X-Dev-User": "casey@example.com"})
    assert r.status_code == 200
    assert "Estimating mailbox" in r.text and "down" in r.text
    assert "Manual upload" in r.text


def test_admin_upload_eml_ingests_it(client, session, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from bidtriage.core.blobs import LocalBlobStore
    from bidtriage.core.models import RawMessage, Source

    _seed(session)
    monkeypatch.setattr(
        "bidtriage.web.routers.admin.get_blob_store", lambda: LocalBlobStore(tmp_path / "blobs")
    )
    raw = (
        b"Message-ID: <upload-1@gc.com>\r\nFrom: Bob <bbuilder@mascaroconstruction.com>\r\n"
        b"To: dana@ferryelectric.com\r\nSubject: ITB - Wexford MOB\r\n"
        b"Date: Tue, 29 Sep 2026 16:45:00 -0400\r\n\r\nBids due 10/20 at 2 PM.\r\n"
    )
    r = client.post(
        "/admin/upload",
        files={"file": ("itb.eml", raw, "message/rfc822")},
        headers={"X-Dev-User": "casey@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "uploaded=new" in r.headers["location"]
    msg = session.scalars(select(RawMessage)).one()
    assert msg.internet_message_id == "<upload-1@gc.com>"
    assert msg.from_addr == "bbuilder@mascaroconstruction.com"
    src = session.scalars(select(Source).where(Source.kind == "manual")).one()
    assert src.paused and src.backfill_done  # nothing to poll, nothing to backfill

    again = client.post(
        "/admin/upload",
        files={"file": ("itb.eml", raw, "message/rfc822")},
        headers={"X-Dev-User": "casey@example.com"},
        follow_redirects=False,
    )
    assert "uploaded=duplicate" in again.headers["location"]
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1


def test_admin_upload_rejects_other_file_types(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    r = client.post(
        "/admin/upload",
        files={"file": ("drawings.pdf", b"%PDF-1.4", "application/pdf")},
        headers={"X-Dev-User": "casey@example.com"},
    )
    assert r.status_code == 400


def test_admin_can_pause_and_resume_a_source(client, session):  # type: ignore[no-untyped-def]
    from bidtriage.core.models import Source

    _seed(session)
    src = Source(id="s2", kind="imap", name="Dana inbox", mailbox="dana@ferryelectric.com")
    session.add(src)
    session.flush()
    for expected in (True, False):
        r = client.post(
            "/admin/sources/s2/pause",
            headers={"X-Dev-User": "casey@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and src.paused is expected


def test_upload_size_cap(client, session, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """An oversize upload is refused with 413 before the whole body is resident (SPEC-10 F9)."""
    from bidtriage.core.blobs import LocalBlobStore
    from bidtriage.core.config import Settings

    _seed(session)
    monkeypatch.setattr(
        "bidtriage.web.routers.admin.get_blob_store", lambda: LocalBlobStore(tmp_path / "blobs")
    )
    monkeypatch.setattr(
        "bidtriage.web.routers.admin.get_settings",
        lambda: Settings(SECRET_KEY=SECRET, BIDTRIAGE_ENV="dev", INGEST_MAX_UPLOAD_BYTES=4096),
    )
    body = b"Message-ID: <big@gc.com>\r\nFrom: gc@x.com\r\nSubject: ITB\r\n\r\n" + b"x" * 20000
    r = client.post(
        "/admin/upload",
        files={"file": ("big.eml", body, "message/rfc822")},
        headers={"X-Dev-User": "casey@example.com"},
    )
    assert r.status_code == 413
    from sqlalchemy import select

    from bidtriage.core.models import RawMessage

    assert session.scalars(select(RawMessage)).all() == []


def test_upload_rejects_empty_file(client, session, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    from bidtriage.core.blobs import LocalBlobStore

    _seed(session)
    monkeypatch.setattr(
        "bidtriage.web.routers.admin.get_blob_store", lambda: LocalBlobStore(tmp_path / "blobs")
    )
    r = client.post(
        "/admin/upload",
        files={"file": ("empty.eml", b"", "message/rfc822")},
        headers={"X-Dev-User": "casey@example.com"},
    )
    assert r.status_code == 400


def _failed_message(session, **kw):  # type: ignore[no-untyped-def]
    from bidtriage.core.models import RawMessage

    fields = dict(
        content_hash="h" * 64,
        from_addr="jdoe@pjdick.com",
        from_name="Jane Doe",
        subject="ITB - Benedum Hall Lab Renovation - Electrical",
        received_at=datetime.now(tz=UTC),
        created_at=datetime.now(tz=UTC),
        extraction_status="failed",
        extraction_attempts=3,
        extraction_error="error: ConnectionError: anthropic unreachable",
    )
    fields.update(kw)
    msg = RawMessage(**fields)
    session.add(msg)
    session.flush()
    return msg


def test_review_page_lists_failed_extractions(client, session):  # type: ignore[no-untyped-def]
    msg = _failed_message(session)
    body = client.get("/admin/").text
    assert "Needs review (1)" in body
    assert "ITB - Benedum Hall Lab Renovation" in body
    assert "anthropic unreachable" in body
    assert f"/admin/messages/{msg.id}/reextract" in body


def test_review_page_lists_low_confidence_classifications(client, session):  # type: ignore[no-untyped-def]
    _failed_message(
        session,
        extraction_status="done",
        extraction_attempts=1,
        extraction_error=None,
        kind="itb",
        kind_confidence=0.41,
        subject="Scanned ITB - see attached (fax copy)",
    )
    body = client.get("/admin/").text
    assert "Needs review (1)" in body and "0.41" in body


def test_reextract_queues_a_job_and_audits_it(client, session):  # type: ignore[no-untyped-def]
    from bidtriage.core.models import AuditEvent, Job

    msg = _failed_message(session)
    r = client.post(f"/admin/messages/{msg.id}/reextract", follow_redirects=False)
    assert r.status_code == 303 and "#review" in r.headers["location"]
    jobs = session.scalars(select(Job)).all()
    assert [j.kind for j in jobs] == ["reextract_message"]
    assert jobs[0].payload == {"message_id": msg.id}
    actions = [a.action for a in session.scalars(select(AuditEvent)).all()]
    assert "message.reextract" in actions


def test_reextract_unknown_message_is_a_404(client, session):  # type: ignore[no-untyped-def]
    assert client.post("/admin/messages/nope/reextract").status_code == 404


def test_reextract_requires_a_known_role(client, session):  # type: ignore[no-untyped-def]
    msg = _failed_message(session)
    session.add(User(id="u9", email="guest@example.com", name="Guest", role="viewer"))
    session.flush()
    r = client.post(
        f"/admin/messages/{msg.id}/reextract",
        headers={"X-Dev-User": "guest@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# ----------------------------------------------------------- SPEC-03 F6 on the review page


def _pair(session):  # type: ignore[no-untyped-def]
    """Two opportunities, the second flagged as a possible duplicate of the first.

    `normalized_name` matters: it is what candidate generation pre-filters on, and resolution
    always populates it (SPEC-03 technical notes).
    """
    _, first = _seed(session)
    first.normalized_name = "benedum hall"
    first.canonical = {**first.canonical, "gc_domain": "pjdick.com", "scope_items": []}
    other = Opportunity(
        id="opp2",
        status="new",
        canonical={
            "project_name": {"value": "Benedum Hall Lab", "confidence": 0.9},
            "gc_name": {"value": "PJ Dick", "confidence": 0.9},
            "gc_domain": "pjdick.com",
            "bid_due": {"value": "2026-10-16T14:00:00-04:00"},
            "location": {"city": "Pittsburgh"},
            "scope_items": [],
        },
        normalized_name="benedum hall lab",
        flags=["possible_duplicate"],
        first_seen_at=datetime.now(tz=UTC),
        last_activity_at=datetime.now(tz=UTC),
    )
    session.add(other)
    session.flush()
    return other


def test_review_page_lists_possible_duplicates(client, session):  # type: ignore[no-untyped-def]
    _pair(session)
    body = client.get("/").text
    assert "Needs review" in body and "possible duplicate" in body


def test_duplicates_endpoint_ranks_candidates(client, session):  # type: ignore[no-untyped-def]
    _pair(session)
    rows = client.get("/api/opportunities/opp2/duplicates").json()
    assert [r["id"] for r in rows] == ["opp1"]
    assert 0 < rows[0]["score"] <= 1.0
    assert client.get("/api/opportunities/nope/duplicates").status_code == 404


def test_merge_then_undo_over_the_api(client, session):  # type: ignore[no-untyped-def]
    _pair(session)
    r = client.post("/api/opportunities/opp1/merge", json={"victim_id": "opp2"})
    assert r.status_code == 200, r.text
    log_id = r.json()["merge_log_id"]
    assert session.get(Opportunity, "opp2") is None
    assert [m["id"] for m in client.get("/api/merges").json()] == [log_id]
    assert client.post(f"/api/merges/{log_id}/undo").status_code == 200
    assert session.get(Opportunity, "opp2") is not None
    # A second undo is refused rather than applied twice.
    assert client.post(f"/api/merges/{log_id}/undo").status_code == 400


def test_merge_conflicting_decisions_is_a_409(client, session):  # type: ignore[no-untyped-def]
    other = _pair(session)
    session.get(Opportunity, "opp1").status = "bidding"
    other.status = "passed"
    session.flush()
    r = client.post("/api/opportunities/opp1/merge", json={"victim_id": "opp2"})
    assert r.status_code == 409 and "before merging" in r.json()["detail"]
    assert session.get(Opportunity, "opp2") is not None


def test_merge_unknown_opportunity_is_a_404(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    assert (
        client.post("/api/opportunities/opp1/merge", json={"victim_id": "nope"}).status_code == 404
    )


def test_split_requires_more_than_one_source(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    assert (
        client.post("/api/opportunities/opp1/split", json={"message_id": "nope"}).status_code == 404
    )


def test_curation_requires_a_known_role(client, session):  # type: ignore[no-untyped-def]
    _pair(session)
    session.get(User, "u1").role = "readonly"
    session.flush()
    r = client.post("/api/opportunities/opp1/merge", json={"victim_id": "opp2"})
    assert r.status_code == 403


# ------------------------------------------------- SPEC-04 F7: the scoring profile editor


def _profile_form(session, **overrides):  # type: ignore[no-untyped-def]
    """The form the editor renders: one `p.<dotted.path>` field per leaf of the profile JSON."""
    from bidtriage.worker.pipeline import active_profile

    profile, version = active_profile(session)
    session.flush()
    form: dict[str, str] = {"based_on": str(version), "note": "test"}

    def walk(prefix: str, obj: dict) -> None:
        for k, v in obj.items():
            if isinstance(v, dict):
                walk(f"{prefix}{k}.", v)
            elif isinstance(v, list):
                form[f"p.{prefix}{k}"] = ", ".join(str(x) for x in v)
            else:
                form[f"p.{prefix}{k}"] = "" if v is None else str(v)

    walk("", profile.model_dump(mode="json"))
    form.update(overrides)
    return form


def test_profile_editor_renders_every_field(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    r = client.get("/admin/profiles/edit")
    assert r.status_code == 200
    assert 'name="p.weights.project_type"' in r.text
    assert 'name="p.type_table.higher_education"' in r.text
    assert 'name="p.size_band.sweet_high"' in r.text
    assert 'name="p.thresholds.bid"' in r.text
    assert 'name="p.boosts.key_account"' in r.text


def test_profile_schema_is_published(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    schema = client.get("/admin/profiles/schema.json").json()
    assert schema["title"] == "Profile"
    assert "type_table" in schema["properties"]


def test_profile_edit_round_trips_and_saves_a_new_version(client, session):  # type: ignore[no-untyped-def]
    from bidtriage.core.models import ScoringProfile

    _seed(session)
    form = _profile_form(
        session, **{"p.type_table.higher_education": "0.8", "p.type_table.healthcare": "1.0"}
    )
    r = client.post("/admin/profiles", data=form, follow_redirects=False)
    assert r.status_code == 303
    saved = session.scalars(select(ScoringProfile).order_by(ScoringProfile.version.desc())).first()
    assert saved is not None and saved.json["type_table"]["higher_education"] == 0.8
    # A draft is inert until someone activates it.
    assert saved.active is False
    # Nothing else drifted on the way through the form.
    assert saved.json["weights"] == {
        "project_type": 0.25,
        "size": 0.25,
        "gc": 0.25,
        "distance": 0.10,
        "timing": 0.10,
        "bid_type": 0.05,
    }


def test_profile_edit_rejects_weights_that_do_not_sum_to_one(client, session):  # type: ignore[no-untyped-def]
    from bidtriage.core.models import ScoringProfile

    _seed(session)
    form = _profile_form(session, **{"p.weights.project_type": "0.30"})  # sums to 1.05
    before = session.scalar(select(func.count()).select_from(ScoringProfile))
    r = client.post("/admin/profiles", data=form, follow_redirects=False)
    assert r.status_code == 400 and "sum to 1.0" in r.json()["detail"]
    assert session.scalar(select(func.count()).select_from(ScoringProfile)) == before


def test_profile_preview_shows_band_changes_and_saves_nothing(client, session):  # type: ignore[no-untyped-def]
    from bidtriage.core.models import Score, ScoringProfile
    from bidtriage.worker import pipeline

    user, opp = _seed(session)
    opp.canonical = {
        **opp.canonical,
        "project_type": "higher_education",
        "size_signals": {"stated_electrical_value": 194_000},
        "bid_type": "hard_bid",
        "sector": "private",
        "bid_due": {"value": (datetime.now(tz=UTC) + timedelta(days=14)).isoformat()},
    }
    session.flush()
    pipeline.rescore(session, [opp], now=datetime.now(tz=UTC))
    versions = session.scalar(select(func.count()).select_from(ScoringProfile))
    scores = session.scalar(select(func.count()).select_from(Score))

    form = _profile_form(
        session, **{"p.type_table.higher_education": "0.8", "p.type_table.healthcare": "1.0"}
    )
    r = client.post("/admin/profiles/preview", data=form)
    assert r.status_code == 200
    assert "would change band" in r.text and "Benedum Hall" in r.text
    # A preview is a question, not an edit.
    assert session.scalar(select(func.count()).select_from(ScoringProfile)) == versions
    assert session.scalar(select(func.count()).select_from(Score)) == scores


def test_profile_preview_reports_validation_errors_in_the_form(client, session):  # type: ignore[no-untyped-def]
    _seed(session)
    form = _profile_form(session, **{"p.weights.size": "0.9"})
    r = client.post("/admin/profiles/preview", data=form)
    assert r.status_code == 200 and "sum to 1.0" in r.text


def test_profile_warnings_name_a_missing_type(client, session):  # type: ignore[no-untyped-def]
    """A type with no row still scores, as `other`. The editor has to say so out loud."""
    import json as _json

    from bidtriage.core.models import ScoringProfile
    from bidtriage.scoring.profile import DEFAULT_PROFILE

    _seed(session)
    table = dict(DEFAULT_PROFILE.type_table)
    del table["healthcare"]
    body = DEFAULT_PROFILE.model_dump(mode="json") | {"type_table": table}
    r = client.post(
        "/admin/profiles",
        data={"json_body": _json.dumps(body), "note": "dropped healthcare"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    saved = session.scalars(select(ScoringProfile).order_by(ScoringProfile.version.desc())).first()
    assert saved is not None

    page = client.get(f"/admin/profiles/edit?version={saved.version}")
    assert page.status_code == 200
    assert "healthcare" in page.text and "fall back to other" in page.text


def test_opportunity_page_shows_what_each_factor_is_worth(client, session):  # type: ignore[no-untyped-def]
    """An estimator has to be able to see why a factor was left out of the Why line."""
    from bidtriage.worker import pipeline

    _seed(session)
    opp = session.get(Opportunity, "opp1")
    assert opp is not None
    opp.canonical = {**opp.canonical, "project_type": "government_civic"}
    session.flush()
    pipeline.rescore(session, [opp], now=datetime.now(tz=UTC))

    r = client.get("/opportunities/opp1")
    assert r.status_code == 200
    assert "Neutral" in r.text
    assert "no signal" in r.text  # size and GC are unknown, and say so
    assert "helping" in r.text  # government civic is above neutral
