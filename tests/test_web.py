"""Web layer smoke tests with the SQLite session injected (SPEC-06 F2 pre-fetch safety, SPEC-09 health)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func

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
