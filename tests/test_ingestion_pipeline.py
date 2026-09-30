"""SPEC-01 acceptance criteria: storage, dedupe, blobs, health alerting and backfill."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from bidtriage.core.blobs import LocalBlobStore, blob_key
from bidtriage.core.jobs import claim, enqueue
from bidtriage.core.models import (
    MessageLink,
    MessageSource,
    RawAttachment,
    RawMessage,
    Source,
    SourcePoll,
    User,
)
from bidtriage.ingestion.eml import parse_eml
from bidtriage.ingestion.protocol import PollResult
from bidtriage.worker import ingest_job, pipeline
from bidtriage.worker.handlers import Context, poll_source
from tests.helpers import blank_pdf, tiny_pdf, zip_bytes

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- fixtures


class StubSource:
    """A source whose batches are scripted, so poll/backfill behaviour is deterministic."""

    def __init__(self, batches: list[list[tuple[str, bytes]]], *, errors: list[str] | None = None):
        self.batches = batches
        self.errors = errors or []
        self.calls = 0
        self.backfill_calls: list[datetime] = []

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        return PollResult(
            [(pid, parse_eml(raw)) for pid, raw in batch],
            {**state, "calls": self.calls},
            list(self.errors),
            more_available=self.calls < len(self.batches),
        )

    def backfill(self, state: dict[str, Any], *, since: datetime, limit: int = 50) -> PollResult:
        self.backfill_calls.append(since)
        return self.poll(state, limit=limit)


@pytest.fixture
def blobs(tmp_path):  # type: ignore[no-untyped-def]
    return LocalBlobStore(tmp_path / "blobs")


@pytest.fixture
def ctx(blobs):  # type: ignore[no-untyped-def]
    return Context(None, blobs=blobs)


def _source(session, kind: str = "imap", mailbox: str = "estimating@ferryelectric.com") -> Source:
    src = Source(kind=kind, name=f"{mailbox} ({kind})", mailbox=mailbox, backfill_done=True)
    session.add(src)
    session.flush()
    return src


def _eml(
    mid: str,
    *,
    subject: str = "ITB - Benedum Center Renovation",
    to: str = "estimating@ferryelectric.com",
    cc: str = "",
    body: str = "Bids due 10/20 at 2 PM.",
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    import base64

    headers = (
        f"Message-ID: <{mid}>\r\nFrom: Bob Builder <bbuilder@mascaroconstruction.com>\r\n"
        f"To: {to}\r\nSubject: {subject}\r\nDate: Tue, 29 Sep 2026 16:45:00 -0400\r\n"
    )
    if cc:
        headers += f"Cc: {cc}\r\n"
    if not attachments:
        return (headers + f"\r\n{body}\r\n").encode()
    out = headers + 'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="B"\r\n\r\n'
    out += f"--B\r\nContent-Type: text/plain\r\n\r\n{body}\r\n"
    for filename, mime, data in attachments:
        out += (
            f"--B\r\nContent-Type: {mime}\r\n"
            f'Content-Disposition: attachment; filename="{filename}"\r\n'
            f"Content-Transfer-Encoding: base64\r\n\r\n{base64.b64encode(data).decode()}\r\n"
        )
    return (out + "--B--\r\n").encode()


# ---------------------------------------------------------------- AC1 / AC2


def test_poll_stores_three_messages_then_finds_them_all_duplicate(session, ctx):
    src = _source(session)
    batch = [(f"m{i}", _eml(f"m{i}@gc.com", subject=f"ITB Project {i}")) for i in range(3)]
    impl = StubSource([batch, batch])

    first = ingest_job.run_poll(session, src, impl, ctx)
    assert (first.seen, first.new, first.duplicates) == (3, 3, 0)
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 3
    stored = session.scalars(select(RawMessage)).all()
    assert all(
        m.body_text and m.headers and m.from_addr == "bbuilder@mascaroconstruction.com"
        for m in stored
    )
    assert src.last_success_at is not None and src.status == "active"

    second = ingest_job.run_poll(session, src, impl, ctx)
    assert (second.seen, second.new, second.duplicates) == (3, 0, 3)
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 3
    polls = session.scalars(select(SourcePoll).order_by(SourcePoll.started_at)).all()
    assert len(polls) == 2 and polls[1].new == 0 and polls[1].duplicates == 3
    assert all(p.finished_at is not None and p.mode == "live" for p in polls)


# ---------------------------------------------------------------- AC3


def test_cc_fanout_is_one_message_with_three_recipient_paths(session, ctx):
    """One ITB, three connected mailboxes: one record, copies=3, three paths (SPEC-01 F3)."""
    raw = _eml(
        "fanout@gc.com",
        to="estimating@ferryelectric.com",
        cc="casey@ferryelectric.com, dana@ferryelectric.com",
    )
    mailboxes = [
        "estimating@ferryelectric.com",
        "casey@ferryelectric.com",
        "dana@ferryelectric.com",
    ]
    for mailbox in mailboxes:
        src = _source(session, mailbox=mailbox)
        ingest_job.run_poll(session, src, StubSource([[("pid-1", raw)]]), ctx)

    msgs = session.scalars(select(RawMessage)).all()
    assert len(msgs) == 1 and msgs[0].copies == 3
    paths = session.scalars(
        select(MessageSource.recipient_path).where(MessageSource.message_id == msgs[0].id)
    ).all()
    assert sorted(paths) == sorted(mailboxes)


def test_cross_provider_dedupe(session, ctx):
    """The same mailbox reached over Graph and IMAP: one record, two source paths."""
    raw = _eml("same@gc.com")
    graph = _source(session, kind="graph")
    imap = _source(session, kind="imap")
    ingest_job.run_poll(session, graph, StubSource([[("AAMkAD-graph-id", raw)]]), ctx)
    ingest_job.run_poll(session, imap, StubSource([[("INBOX:100:7", raw)]]), ctx)

    msgs = session.scalars(select(RawMessage)).all()
    assert len(msgs) == 1 and msgs[0].copies == 2
    links = session.scalars(
        select(MessageSource).where(MessageSource.message_id == msgs[0].id)
    ).all()
    assert {lk.source_id for lk in links} == {graph.id, imap.id}
    assert {lk.provider_message_id for lk in links} == {"AAMkAD-graph-id", "INBOX:100:7"}


def test_different_attachments_are_not_duplicates(session, ctx):
    """Identical subject and body but different attachments: two records (SPEC-01 F3)."""
    src = _source(session)
    a = _eml("no-id-a@x", attachments=[("ITB.pdf", "application/pdf", tiny_pdf("version A"))])
    b = _eml("no-id-b@x", attachments=[("ITB.pdf", "application/pdf", tiny_pdf("version B"))])
    ingest_job.run_poll(session, src, StubSource([[("p1", a), ("p2", b)]]), ctx)
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2


def test_content_hash_dedupe_expires_after_seven_days(session, ctx, blobs):
    """Rule 3 is scoped to a 7-day window so an annual re-bid is not swallowed (SPEC-01 F3)."""
    raw = _eml("no-message-id-here@x")
    parsed = parse_eml(raw)
    parsed.internet_message_id = None
    src = _source(session)

    class At:
        def __init__(self, at: datetime) -> None:
            self.at = at

        def now(self) -> datetime:
            return self.at

    pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="a",
        parsed=parsed,
        clock=At(NOW),
        blobs=blobs,
    )
    _, is_new_soon = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="b",
        parsed=parsed,
        clock=At(NOW + timedelta(days=3)),
        blobs=blobs,
    )
    _, is_new_later = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="c",
        parsed=parsed,
        clock=At(NOW + timedelta(days=8)),
        blobs=blobs,
    )
    assert not is_new_soon and is_new_later


# ---------------------------------------------------------------- AC4


def test_forwarded_message_records_original_sender_and_forwarder(session, ctx, fixtures_dir):
    dana = User(email="dana@ferryelectric.com", name="Dana Estimator", role="estimator")
    session.add(dana)
    session.flush()
    src = _source(session, mailbox="itb@bidtriage.ferryelectric.com")
    raw = (fixtures_dir / "forward_inline_mascaro.eml").read_bytes()
    ingest_job.run_poll(session, src, StubSource([[("fwd-1", raw)]]), ctx)

    msg = session.scalars(select(RawMessage)).one()
    assert msg.from_addr == "bbuilder@mascaroconstruction.com"
    assert msg.sent_at is not None and msg.sent_at.astimezone(UTC).day == 29
    assert msg.forwarded_by_addr == "dana@ferryelectric.com"
    assert msg.forwarded_by_user_id == dana.id
    assert msg.forward_note == "Casey, worth a look? Mascaro job, sounds like our kind of thing."
    assert msg.forward_chain == ["dana@ferryelectric.com"]
    assert msg.sent_at_confidence == "low"


def test_unknown_forwarder_is_recorded_by_address_only(session, ctx, fixtures_dir):
    src = _source(session)
    raw = (fixtures_dir / "forward_inline_mascaro.eml").read_bytes()
    ingest_job.run_poll(session, src, StubSource([[("fwd-1", raw)]]), ctx)
    msg = session.scalars(select(RawMessage)).one()
    assert msg.forwarded_by_user_id is None
    assert msg.forwarded_by_addr == "dana@ferryelectric.com"


# ---------------------------------------------------------------- AC5 / AC6 / AC7 / AC8


def test_pdf_attachment_text_has_page_markers(session, ctx):
    src = _source(session)
    letter = tiny_pdf(*[f"page {i} of the invitation to bid" for i in range(1, 7)])
    raw = _eml("pdf@gc.com", attachments=[("ITB Letter.pdf", "application/pdf", letter)])
    ingest_job.run_poll(session, src, StubSource([[("p1", raw)]]), ctx)

    att = session.scalars(select(RawAttachment)).one()
    assert att.text is not None and att.pages == 6
    assert "[page 1]" in att.text and "[page 6]" in att.text
    assert "page 6 of the invitation to bid" in att.text
    assert att.blob_key == blob_key(att.sha256) and not att.large_document


def test_large_drawing_set_is_stored_without_text(session, ctx, blobs):
    src = _source(session)
    drawings = tiny_pdf(*["sheet"] * 120)
    raw = _eml("dwg@gc.com", attachments=[("E-Series.pdf", "application/pdf", drawings)])
    ingest_job.run_poll(session, src, StubSource([[("p1", raw)]]), ctx)

    att = session.scalars(select(RawAttachment)).one()
    assert att.text is None and att.large_document and att.pages == 120
    assert att.blob_key is not None and blobs.exists(att.blob_key)


def test_scanned_pdf_is_ocred(session, blobs):
    class FakeOcr:
        def page_texts(self, pdf: bytes, max_pages: int) -> list[str]:
            return ["INVITATION TO BID", "Bids due October 20 at 2 PM"][:max_pages]

    src = _source(session)
    raw = _eml("scan@gc.com", attachments=[("Scan.pdf", "application/pdf", blank_pdf(2))])
    pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="p1",
        parsed=parse_eml(raw),
        ocr=FakeOcr(),
        blobs=blobs,
    )
    att = session.scalars(select(RawAttachment)).one()
    assert att.ocr and att.text is not None and "INVITATION TO BID" in att.text
    assert att.extraction_error is None


def test_zip_members_become_child_attachments(session, ctx):
    src = _source(session)
    payload = zip_bytes(
        {
            "ITB Letter.pdf": tiny_pdf("Bids due 10/20"),
            "Scope.pdf": tiny_pdf("Division 26"),
            "Bid Form.pdf": tiny_pdf("Base bid"),
            "Drawings.zip": zip_bytes({"E1.pdf": tiny_pdf("not opened")}),
        }
    )
    raw = _eml("zip@gc.com", attachments=[("Bid Package.zip", "application/zip", payload)])
    ingest_job.run_poll(session, src, StubSource([[("p1", raw)]]), ctx)

    rows = session.scalars(select(RawAttachment)).all()
    container = next(r for r in rows if r.parent_id is None)
    children = [r for r in rows if r.parent_id == container.id]
    assert container.mime == "application/zip"
    assert sorted(c.filename for c in children) == ["Bid Form.pdf", "ITB Letter.pdf", "Scope.pdf"]
    assert all(c.text and "[page 1]" in c.text for c in children)
    assert container.text is not None and "Drawings.zip" in container.text
    assert not any(c.filename == "E1.pdf" for c in children)


def test_oversize_attachment_keeps_metadata_but_no_blob(session, blobs):
    class Huge(bytes):
        def __len__(self) -> int:
            return 201 * 1024 * 1024

    src = _source(session)
    parsed = parse_eml(_eml("big@gc.com"))
    from bidtriage.ingestion.eml import ParsedAttachment

    parsed.attachments = [ParsedAttachment("Drawings.pdf", "application/pdf", Huge(b"%PDF-1.4"))]
    pipeline.ingest_parsed(
        session, source_id=src.id, provider_message_id="p1", parsed=parsed, blobs=blobs
    )
    att = session.scalars(select(RawAttachment)).one()
    assert att.oversize and att.extraction_error == "oversize"
    assert att.blob_key is None and att.text is None and att.filename == "Drawings.pdf"


def test_identical_attachments_share_one_blob(session, ctx, blobs, tmp_path):
    """Blobs are addressed by SHA-256, so the same letter sent twice is stored once."""
    src = _source(session)
    letter = tiny_pdf("Bids due 10/20")
    a = _eml("one@gc.com", subject="ITB A", attachments=[("ITB.pdf", "application/pdf", letter)])
    b = _eml("two@gc.com", subject="ITB B", attachments=[("ITB.pdf", "application/pdf", letter)])
    ingest_job.run_poll(session, src, StubSource([[("p1", a), ("p2", b)]]), ctx)

    rows = session.scalars(select(RawAttachment)).all()
    assert len(rows) == 2 and len({r.blob_key for r in rows}) == 1
    assert len(list((tmp_path / "blobs").rglob("*"))) == 3  # two fan-out dirs + one file


# ---------------------------------------------------------------- AC9


def test_safelinks_wrapped_url_is_unwrapped_on_ingest(session, ctx):
    src = _source(session)
    wrapped = (
        "https://nam02.safelinks.protection.outlook.com/?url="
        "https%3A%2F%2Fapp.buildingconnected.com%2Fprojects%2Fabc%2Frfp&data=05"
    )
    raw = _eml("link@gc.com", body=f"Bid here: {wrapped}")
    ingest_job.run_poll(session, src, StubSource([[("p1", raw)]]), ctx)

    link = session.scalars(select(MessageLink)).one()
    assert link.url == "https://app.buildingconnected.com/projects/abc/rfp"
    assert link.host_class == "buildingconnected" and not link.wrapped


def test_links_in_attachment_text_are_harvested(session, ctx):
    src = _source(session)
    letter = tiny_pdf("Plan room: https://mascaro.egnyte.com/fl/AHNWexford")
    raw = _eml(
        "att-link@gc.com",
        body="See attached.",
        attachments=[("ITB.pdf", "application/pdf", letter)],
    )
    ingest_job.run_poll(session, src, StubSource([[("p1", raw)]]), ctx)
    classes = session.scalars(select(MessageLink.host_class)).all()
    assert "egnyte" in classes


# ---------------------------------------------------------------- AC10: health and alerting


def test_source_down_alerts_once_and_rearms(session, ctx):
    session.add(User(email="admin@ferryelectric.com", name="Admin", role="admin"))
    src = _source(session)
    src.last_success_at = NOW - timedelta(minutes=90)
    session.flush()

    sent: list[str] = []

    def mailer(*, to: str, subject: str, html: str, text: str) -> str:
        sent.append(subject)
        return "<id>"

    for _ in range(3):  # three 5-minute polls while the token stays revoked
        ingest_job.check_sources(session, ctx, now=NOW, mailer=mailer)
    assert len(sent) == 1 and "mail source down" in sent[0]
    assert src.down_alert_sent_at is not None

    src.last_success_at = NOW  # recovered
    ingest_job.check_sources(session, ctx, now=NOW, mailer=mailer)
    assert src.down_alert_sent_at is None
    src.last_success_at = NOW - timedelta(minutes=90)  # and down again
    ingest_job.check_sources(session, ctx, now=NOW, mailer=mailer)
    assert len(sent) == 2


def test_down_source_appears_in_digest_health(session, ctx):
    from bidtriage.worker.digest_job import health_lines

    src = _source(session)
    src.last_success_at = NOW - timedelta(minutes=90)
    session.flush()
    lines = health_lines(session, NOW)
    assert [(lk.status, bool(lk.detail)) for lk in lines] == [("down", True)]
    assert src.name in lines[0].name


def test_paused_source_is_not_alerted(session, ctx):
    session.add(User(email="admin@ferryelectric.com", name="Admin", role="admin"))
    src = _source(session)
    src.paused = True
    session.flush()
    sent: list[str] = []
    ingest_job.check_sources(
        session, ctx, now=NOW, mailer=lambda **kw: sent.append(kw["subject"]) or "<id>"
    )
    assert sent == [] and src.down_alert_sent_at is None


def test_failed_poll_still_writes_a_poll_row(session, ctx):
    class Broken:
        def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
            raise RuntimeError("401 unauthorized: token revoked")

    src = _source(session)
    src.last_success_at = NOW
    session.flush()
    with pytest.raises(RuntimeError):
        ingest_job.run_poll(session, src, Broken(), ctx)
    poll = session.scalars(select(SourcePoll)).one()
    assert poll.finished_at is not None and "token revoked" in poll.errors[0]
    assert src.status == "error" and src.last_success_at == NOW  # success time is not advanced


def test_per_message_errors_do_not_make_a_reachable_source_look_down(session, ctx):
    """A poll that reached the mailbox counts as a success even if one message was unreadable.

    Otherwise a single poison message would degrade and then alert on a healthy mailbox.
    """
    src = _source(session)
    impl = StubSource([[("p1", _eml("ok@gc.com"))]], errors=["m9: unreadable MIME"])
    ingest_job.run_poll(session, src, impl, ctx)
    assert src.last_success_at is not None and src.status == "warning"
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1
    poll = session.scalars(select(SourcePoll)).one()
    assert poll.errors == ["m9: unreadable MIME"]


def test_revoked_credentials_lead_to_down_and_one_alert(session, ctx):
    """AC10 end to end: the poll raises, no success is recorded, and 60 minutes later it is down."""
    from bidtriage.ingestion.health import DOWN, source_health

    session.add(User(email="admin@ferryelectric.com", name="Admin", role="admin"))
    src = _source(session)

    class Revoked:
        def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
            raise RuntimeError("AADSTS7000215: invalid client secret")

    for _ in range(12):  # an hour of five-minute polls
        with pytest.raises(RuntimeError):
            ingest_job.run_poll(session, src, Revoked(), ctx)
    assert src.last_success_at is None
    assert source_health(last_success_at=src.last_success_at, now=NOW).status == DOWN

    sent: list[str] = []
    for _ in range(3):
        ingest_job.check_sources(
            session, ctx, now=NOW, mailer=lambda **kw: sent.append(kw["subject"]) or "<id>"
        )
    assert len(sent) == 1
    assert len(session.scalars(select(SourcePoll)).all()) == 12


# ---------------------------------------------------------------- AC11: backfill


def test_live_cursor_is_seeded_so_the_first_poll_does_not_drain_history(session, ctx):
    """A new source starts live polling at *now*; the 90-day window is the backfill's job (F7)."""

    class Seedable(StubSource):
        def __init__(self, batches):  # type: ignore[no-untyped-def]
            super().__init__(batches)
            self.seeded = 0

        def seed(self, state: dict[str, Any]) -> dict[str, Any]:
            self.seeded += 1
            return {"delta:inbox": "token-at-now"}

    src = _source(session)
    src.delta_state = {}
    src.backfill_days = 90
    session.flush()
    impl = Seedable([[("live-1", _eml("live@gc.com"))]])

    ingest_job.run_poll(session, src, impl, ctx)
    assert impl.seeded == 1 and src.delta_state["calls"] == 1
    ingest_job.run_poll(session, src, impl, ctx)
    assert impl.seeded == 1  # seeded once, not on every poll


def test_live_cursor_is_not_seeded_when_backfill_is_disabled(session, ctx):
    class Seedable(StubSource):
        def __init__(self, batches):  # type: ignore[no-untyped-def]
            super().__init__(batches)
            self.seeded = 0

        def seed(self, state: dict[str, Any]) -> dict[str, Any]:
            self.seeded += 1
            return {"delta:inbox": "token-at-now"}

    src = _source(session)
    src.delta_state = {}
    src.backfill_days = 0
    session.flush()
    impl = Seedable([[("live-1", _eml("live@gc.com"))]])
    ingest_job.run_poll(session, src, impl, ctx)
    assert impl.seeded == 0


def test_backfill_runs_behind_live_polls(session, ctx):
    """poll_source enqueues the backfill at a lower priority, so live mail is claimed first."""
    src = _source(session)
    src.backfill_done = False
    session.flush()
    impl = StubSource([[("live-1", _eml("live@gc.com"))]])
    ctx.sources[src.id] = impl

    poll_source(session, {"source_id": src.id}, ctx)
    session.flush()
    enqueue(session, "poll_source", f"poll:{src.id}:next", {"source_id": src.id}, priority=50)
    assert claim(session).kind == "poll_source"
    assert claim(session).kind == "backfill_source"


def test_backfill_chains_batches_then_finishes(session, ctx):
    src = _source(session)
    src.backfill_done = False
    src.backfill_days = 90
    session.flush()
    old = [[(f"old{i}", _eml(f"old{i}@gc.com", subject=f"ITB {i}"))] for i in range(3)]
    impl = StubSource(old)

    ingest_job.run_backfill(session, src, impl, ctx)
    assert not src.backfill_done and src.delta_state["backfill:batch"] == 1
    ingest_job.run_backfill(session, src, impl, ctx)
    ingest_job.run_backfill(session, src, impl, ctx)
    assert src.backfill_done
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 3
    assert {p.mode for p in session.scalars(select(SourcePoll)).all()} == {"backfill"}
    since = impl.backfill_calls[0]
    assert timedelta(days=89) < datetime.now(tz=UTC) - since < timedelta(days=91)


def test_backfill_is_skipped_for_sources_that_cannot_walk_history(session, ctx):
    class LiveOnly:
        def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
            return PollResult([], state, [])

    src = _source(session)
    src.backfill_done = False
    session.flush()
    ingest_job.run_backfill(session, src, LiveOnly(), ctx)
    assert src.backfill_done


# ---------------------------------------------------------------- crash resume, throughput


def test_crash_resume(session, ctx, monkeypatch):
    """A poll killed mid-batch keeps what it stored; the rerun finishes without duplicates."""
    src = _source(session)
    batch = [(f"m{i}", _eml(f"m{i}@gc.com", subject=f"ITB {i}")) for i in range(3)]
    impl = StubSource([batch, batch])

    real = pipeline.ingest_parsed
    calls = {"n": 0}

    class Killed(BaseException):
        """Stands in for the process dying mid-batch (not caught by the poll's error handling)."""

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise Killed()
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "ingest_parsed", flaky)
    with pytest.raises(Killed):
        ingest_job.run_poll(session, src, impl, ctx)
    session.rollback()  # the worker process is gone; a fresh one starts here
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2

    monkeypatch.setattr(pipeline, "ingest_parsed", real)
    summary = ingest_job.run_poll(session, src, impl, ctx)
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 3
    assert (summary.new, summary.duplicates) == (1, 2)


def test_throughput(session, ctx):
    """Ingestion keeps up with a newsletter-heavy mailbox: >= 5 msgs/sec (SPEC-01 edge cases)."""
    src = _source(session)
    batch = [
        (f"n{i}", _eml(f"n{i}@news.com", subject=f"Newsletter {i}", body=f"Issue {i}. " * 40))
        for i in range(200)
    ]
    started = time.perf_counter()
    summary = ingest_job.run_poll(session, src, StubSource([batch]), ctx)
    elapsed = time.perf_counter() - started
    assert summary.new == 200
    rate = 200 / elapsed
    assert rate >= 5, f"ingested {rate:.1f} msgs/sec"


# ---------------------------------------------------------------- metrics


def test_ingestion_metrics(session, ctx):
    src = _source(session)
    letter = tiny_pdf("Bids due 10/20")
    drawings = tiny_pdf(*["sheet"] * 60)
    batch = [
        (
            "p1",
            _eml(
                "m1@gc.com", subject="ITB A", attachments=[("ITB.pdf", "application/pdf", letter)]
            ),
        ),
        (
            "p2",
            _eml(
                "m2@gc.com", subject="ITB B", attachments=[("E.pdf", "application/pdf", drawings)]
            ),
        ),
    ]
    ingest_job.run_poll(session, src, StubSource([batch, batch]), ctx)
    ingest_job.run_poll(session, src, StubSource([batch]), ctx)
    ingest_job.run_poll(session, src, StubSource([[]], errors=["inbox: throttled"]), ctx)

    m = ingest_job.ingestion_metrics(session)
    assert m.polls == 3 and m.polls_failed == 1
    assert m.poll_success_rate is not None and abs(m.poll_success_rate - 2 / 3) < 1e-9
    assert m.messages_new == 2 and m.duplicates_linked == 2
    assert m.attachments_extracted == 1 and m.attachments_failed == 1
    assert m.lag_p50_seconds is not None and m.lag_p95_seconds is not None


def test_ingestion_metrics_are_empty_before_any_poll(session):
    m = ingest_job.ingestion_metrics(session)
    assert m.polls == 0 and m.poll_success_rate is None and m.lag_p50_seconds is None


# ---------------------------------------------------------------- scheduler wiring


def test_schedule_tick_enqueues_polls_and_health_checks(session, ctx):
    """Poll every 5 minutes per source, plus the health sweep that drives alerting (F7, F8)."""
    from bidtriage.core.models import Job
    from bidtriage.worker.handlers import schedule_tick

    src = _source(session)
    paused = _source(session, mailbox="old@ferryelectric.com")
    paused.paused = True
    session.flush()

    schedule_tick(session, ctx, now=NOW)
    schedule_tick(session, ctx, now=NOW + timedelta(minutes=1))  # same 5-minute bucket
    kinds = [j.kind for j in session.scalars(select(Job)).all()]
    assert kinds.count("poll_source") == 1  # idempotent on the bucket key, and paused is skipped
    assert kinds.count("check_sources") == 1
    payloads = [
        j.payload["source_id"]
        for j in session.scalars(select(Job)).all()
        if j.kind == "poll_source"
    ]
    assert payloads == [src.id]

    schedule_tick(session, ctx, now=NOW + timedelta(minutes=6))  # next bucket
    assert [j.kind for j in session.scalars(select(Job)).all()].count("poll_source") == 2


def test_worker_loop_ingests_through_the_job_queue(session, ctx):
    """The registered handler path, as the worker runs it: claim -> handle -> stored."""
    from bidtriage.core.jobs import complete
    from bidtriage.worker.handlers import HANDLERS

    src = _source(session)
    ctx.sources[src.id] = StubSource([[("m1", _eml("loop@gc.com"))]])
    enqueue(session, "poll_source", f"poll:{src.id}:x", {"source_id": src.id}, priority=50)

    job = claim(session)
    assert job is not None
    HANDLERS[job.kind](session, job.payload, ctx)
    complete(session, job)

    msg = session.scalars(select(RawMessage)).one()
    assert msg.internet_message_id == "<loop@gc.com>"
    assert session.scalars(select(SourcePoll)).one().new == 1
