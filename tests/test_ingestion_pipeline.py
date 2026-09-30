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
from bidtriage.ingestion.protocol import FetchedMessage, FetchOptions, FetchSession
from bidtriage.worker import ingest_job, pipeline
from bidtriage.worker.handlers import Context
from tests.helpers import blank_pdf, tiny_pdf, zip_bytes

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- fixtures


class StubSource:
    """A source whose batches are scripted, so poll and backfill behaviour is deterministic."""

    can_backfill = True

    def __init__(
        self,
        batches: list[list[tuple[str, bytes]]],
        *,
        errors: list[str] | None = None,
        fetch_fails: set[str] | None = None,
    ):
        self.batches = batches
        self.errors = errors or []
        self.fetch_fails = fetch_fails or set()
        self.calls = 0
        self.backfill_calls: list[datetime] = []
        self.seeded = 0

    def fetch(self, state: dict[str, Any], options: FetchOptions) -> StubSession:
        if options.since is not None:
            self.backfill_calls.append(options.since)
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        return StubSession(self, state, options, batch)

    def seed(self, state: dict[str, Any]) -> dict[str, Any]:
        self.seeded += 1
        return {"cursor": "at-now"}


class StubSession(FetchSession):
    def __init__(
        self,
        source: StubSource,
        state: dict[str, Any],
        options: FetchOptions,
        batch: list[tuple[str, bytes]],
    ) -> None:
        super().__init__(state, options)
        self.source = source
        self.batch = batch
        self.errors = list(source.errors)

    def _messages(self):  # type: ignore[no-untyped-def]
        for pid, raw in self.batch:
            if self.pass_over(pid):
                continue
            if pid in self.source.fetch_fails:
                self.fail(pid, "fetch failed")
                continue
            yield FetchedMessage(provider_message_id=pid, parsed=parse_eml(raw))

    def new_state(self) -> dict[str, Any]:
        return {**self.state, "calls": self.source.calls}

    @property
    def more_available(self) -> bool:  # type: ignore[override]
        return self.source.calls < len(self.source.batches)

    @more_available.setter
    def more_available(self, value: bool) -> None:
        self._more = value


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
        can_backfill = False

        def fetch(self, state: dict[str, Any], options: FetchOptions) -> FetchSession:
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
        can_backfill = False

        def fetch(self, state: dict[str, Any], options: FetchOptions) -> FetchSession:
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
    """Jobs are claimed by ascending priority, so a due live poll always wins (SPEC-01 F7)."""
    from bidtriage.core.models import Job
    from bidtriage.worker.handlers import schedule_tick

    src = _source(session)
    src.backfill_done = False
    session.flush()
    ctx.sources[src.id] = StubSource([[("live-1", _eml("live@gc.com"))]])

    schedule_tick(session, ctx, now=NOW)
    kinds = [j.kind for j in session.scalars(select(Job)).all()]
    assert "poll_source" in kinds and "backfill_source" in kinds
    # Claim order is by ascending priority: the cheap health sweep (40), then live mail (50), and
    # the history walk (80) last, so a backfill can never starve live mail.
    claimed = [claim(session).kind for _ in range(4)]
    assert claimed[:3] == ["check_sources", "poll_source", "backfill_source"]
    assert claimed.index("poll_source") < claimed.index("backfill_source")


def test_only_one_backfill_batch_is_queued_per_source(session, ctx):
    """Two outstanding batches for one source would interleave their cursor writes."""
    from bidtriage.core.models import Job
    from bidtriage.worker.handlers import schedule_tick

    src = _source(session)
    src.backfill_done = False
    session.flush()

    schedule_tick(session, ctx, now=NOW)
    schedule_tick(session, ctx, now=NOW + timedelta(minutes=6))  # a later bucket
    queued = [
        j
        for j in session.scalars(select(Job)).all()
        if j.kind == "backfill_source" and j.status == "pending"
    ]
    assert len(queued) == 1


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


def test_seed_requires_backfill_support(session, ctx):
    """A source that cannot walk history must not have its live cursor parked (SPEC-10 F5).

    Seeding it would skip the history *and* leave nothing to ingest it.
    """

    class LiveOnly:
        can_backfill = False

        def __init__(self) -> None:
            self.seeded = 0
            self.fetched = 0

        def seed(self, state: dict[str, Any]) -> dict[str, Any]:
            self.seeded += 1
            return {"cursor": "at-now"}

        def fetch(self, state: dict[str, Any], options: FetchOptions) -> FetchSession:
            self.fetched += 1
            return StubSession(StubSource([[]]), state, options, [])

    impl = LiveOnly()
    src = _source(session)
    src.delta_state = {}
    src.backfill_done = False
    src.backfill_days = 90
    session.flush()

    ingest_job.run_poll(session, src, impl, ctx)
    assert impl.seeded == 0, "not seeded, so the first poll still sees existing mail"
    ingest_job.run_backfill(session, src, impl, ctx)
    assert src.backfill_done, "nothing to walk, so the backfill is complete by definition"


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
    """Ingestion keeps up with a newsletter-heavy mailbox (SPEC-01 edge case).

    Asserted as bounded work — a constant number of queries per message and no batch-sized memory —
    rather than a wall-clock rate, which flakes on a loaded CI runner. A generous time bound is kept
    only to catch an accidental quadratic.
    """
    import tracemalloc

    from sqlalchemy import event

    src = _source(session)
    count = 200
    batch = [
        (f"n{i}", _eml(f"n{i}@news.com", subject=f"Newsletter {i}", body=f"Issue {i}. " * 40))
        for i in range(count)
    ]

    queries = 0

    def count_query(*args: object, **kwargs: object) -> None:
        nonlocal queries
        queries += 1

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count_query)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        summary = ingest_job.run_poll(session, src, StubSource([batch]), ctx)
    finally:
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        event.remove(engine, "before_cursor_execute", count_query)

    assert summary.new == count
    per_message = queries / count
    assert per_message < 20, f"{per_message:.1f} queries per message suggests an N+1"
    assert peak < 32 * 1024 * 1024, f"peak {peak / 1e6:.0f} MB should not scale with the batch"
    assert elapsed < 20, f"ingested {count / elapsed:.1f} msgs/sec"


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


# ---------------------------------------------------------------- SPEC-10 F1: skips


def test_poison_message_is_skipped_and_surfaced(session, ctx):
    """After the attempt limit a message is given up on, the cursor passes it, and it is reviewable."""
    from bidtriage.core.models import SourceSkip

    src = _source(session)
    good = ("ok", _eml("ok@gc.com", subject="ITB good"))
    bad = ("poison", _eml("bad@gc.com", subject="ITB bad"))
    impl = StubSource([[good, bad]] * 10, fetch_fails={"poison"})

    limit = ctx.settings.ingest_max_fetch_attempts
    for _ in range(limit):
        ingest_job.run_poll(session, src, impl, ctx)

    skip = session.get(SourceSkip, {"source_id": src.id, "provider_message_id": "poison"})
    assert skip is not None and skip.attempts == limit and skip.given_up
    assert "fetch failed" in (skip.last_error or "")

    # Given up on, so the next poll passes over it rather than retrying forever...
    ingest_job.run_poll(session, src, impl, ctx)
    assert ingest_job.given_up_ids(session, src.id) == frozenset({"poison"})
    # ...and it is a review item in the digest instead of a silent gap.
    from bidtriage.worker.digest_job import review_items

    kinds = [r.kind for r in review_items(session, "http://x")]
    assert "ingest_skipped" in kinds
    assert session.scalars(select(SourcePoll)).all()[-1].skipped == 1


def test_recovered_message_clears_its_skip_row(session, ctx):
    from bidtriage.core.models import SourceSkip

    src = _source(session)
    batch = [("flaky", _eml("flaky@gc.com"))]
    impl = StubSource([batch, batch], fetch_fails={"flaky"})
    ingest_job.run_poll(session, src, impl, ctx)
    assert (
        session.get(SourceSkip, {"source_id": src.id, "provider_message_id": "flaky"}) is not None
    )

    impl.fetch_fails.clear()
    ingest_job.run_poll(session, src, impl, ctx)
    assert session.get(SourceSkip, {"source_id": src.id, "provider_message_id": "flaky"}) is None
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1


# ---------------------------------------------------------------- SPEC-10 F3


def test_source_impl_rebuilt_on_config_change(session):
    """Rotating a secret takes effect on the next poll, with no worker restart (SPEC-10 F3)."""
    from bidtriage.core.config import get_settings
    from bidtriage.worker.sources import encode_config

    secret = get_settings().secret_key
    src = Source(
        kind="imap",
        name="Estimating",
        mailbox="estimating@ferryelectric.com",
        config_enc=encode_config({"host": "mail.x.com", "password": "old"}, secret),
    )
    session.add(src)
    session.flush()

    ctx = Context(None)
    first = ctx.source_impl(src)
    assert ctx.source_impl(src) is first, "unchanged config reuses the built source"

    src.config_enc = encode_config({"host": "mail.x.com", "password": "rotated"}, secret)
    rebuilt = ctx.source_impl(src)
    assert rebuilt is not first
    assert rebuilt.password == "rotated"


def test_injected_source_impl_is_never_rebuilt(session):
    """A test-injected implementation must survive, or every source test would build a real one."""
    src = _source(session)
    stub = StubSource([[]])
    ctx = Context(None, sources={src.id: stub})
    assert ctx.source_impl(src) is stub
    src.config_enc = "something-else"
    assert ctx.source_impl(src) is stub


# ---------------------------------------------------------------- SPEC-10 F4


def test_duplicate_link_target_is_deterministic(session, ctx, blobs):
    """With several content-hash matches the earliest is always the link target (SPEC-10 F4)."""
    src = _source(session)
    parsed = parse_eml(_eml("dup@gc.com"))
    parsed.internet_message_id = None

    class At:
        def __init__(self, at: datetime) -> None:
            self.at = at

        def now(self) -> datetime:
            return self.at

    first, _ = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="a",
        parsed=parsed,
        clock=At(NOW),
        blobs=blobs,
    )
    # A second row with the same content hash, as a rescan under a different id could create.
    session.add(
        RawMessage(
            id="later",
            content_hash=parsed.content_hash,
            received_at=NOW + timedelta(hours=1),
            created_at=NOW + timedelta(hours=1),
            headers={},
        )
    )
    session.flush()
    linked, is_new = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="c",
        parsed=parsed,
        clock=At(NOW + timedelta(hours=2)),
        blobs=blobs,
    )
    assert not is_new and linked.id == first.id


def test_copies_matches_source_count(session, ctx):
    """`copies` is derived, so it cannot drift from message_sources (SPEC-10 F4)."""
    raw = _eml("fan@gc.com")
    for mailbox in ("estimating@f.com", "casey@f.com", "dana@f.com"):
        src = _source(session, mailbox=mailbox)
        ingest_job.run_poll(session, src, StubSource([[("p", raw)]]), ctx)
    # ...and a re-scan of one mailbox under a new provider id must not inflate it.
    again = session.scalars(select(Source)).first()
    assert again is not None
    ingest_job.run_poll(session, again, StubSource([[("p-rescanned", raw)]]), ctx)

    msg = session.scalars(select(RawMessage)).one()
    distinct = session.scalar(
        select(func.count(func.distinct(MessageSource.source_id))).where(
            MessageSource.message_id == msg.id
        )
    )
    assert msg.copies == distinct == 3


def test_duplicate_message_id_is_rejected_by_the_database(session):
    """The partial unique index is the backstop when two workers race (SPEC-10 F4)."""
    from sqlalchemy.exc import IntegrityError

    for i in (1, 2):
        session.add(
            RawMessage(
                id=f"m{i}",
                internet_message_id="<race@gc.com>",
                content_hash=f"h{i}",
                received_at=NOW,
                created_at=NOW,
                headers={},
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_messages_without_a_message_id_do_not_collide(session):
    """The unique index is partial: the many messages with no Message-ID must still be storable."""
    for i in range(3):
        session.add(
            RawMessage(
                id=f"m{i}",
                internet_message_id=None,
                content_hash=f"h{i}",
                received_at=NOW,
                created_at=NOW,
                headers={},
            )
        )
    session.flush()
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 3


def test_concurrent_dedupe_race(session, ctx, blobs):
    """A racing insert is converted into a duplicate link rather than a second row (SPEC-10 F4)."""
    src_a = _source(session, kind="graph", mailbox="estimating@f.com")
    src_b = _source(session, kind="imap", mailbox="casey@f.com")
    parsed = parse_eml(_eml("race@gc.com"))

    # Stand in for the other worker having committed between our read and our insert.
    real_find = pipeline.find_duplicate
    calls = {"n": 0}

    def find_once_blind(sess, p, *, now):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            session.add(
                RawMessage(
                    id="winner",
                    internet_message_id=parsed.internet_message_id,
                    content_hash=parsed.content_hash,
                    received_at=NOW,
                    created_at=NOW,
                    headers={},
                )
            )
            session.flush()
            return None  # we "saw" no duplicate before the other worker committed
        return real_find(sess, p, now=now)

    pipeline.find_duplicate = find_once_blind  # type: ignore[assignment]
    try:
        msg, is_new = pipeline.ingest_parsed(
            session,
            source_id=src_a.id,
            provider_message_id="p1",
            parsed=parsed,
            blobs=blobs,
        )
    finally:
        pipeline.find_duplicate = real_find  # type: ignore[assignment]

    assert not is_new and msg.id == "winner"
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1
    link = session.scalars(select(MessageSource).where(MessageSource.source_id == src_a.id)).one()
    assert link.message_id == "winner"
    assert src_b is not None


# ---------------------------------------------------------------- SPEC-10 F5


def test_backfill_recovers_after_failed_job(session, ctx):
    """The scheduler re-enqueues a backfill whose chained job exhausted its retries (SPEC-10 F5)."""
    from bidtriage.core.models import Job
    from bidtriage.worker.handlers import schedule_tick

    src = _source(session)
    src.backfill_done = False
    session.flush()

    # The chain is broken: the only chained key was already consumed and its job failed for good.
    session.add(
        Job(
            kind="backfill_source",
            key=f"backfill:{src.id}:0",
            payload={"source_id": src.id},
            status="failed",
            run_at=NOW,
            created_at=NOW,
            attempts=3,
        )
    )
    session.flush()

    schedule_tick(session, ctx, now=NOW)
    pending = [
        j
        for j in session.scalars(select(Job)).all()
        if j.kind == "backfill_source" and j.status == "pending"
    ]
    assert pending, "the tick must recover a broken backfill chain"


def test_backfill_stuck_is_surfaced(session, ctx):
    src = _source(session)
    src.backfill_done = False
    session.flush()
    impl = StubSource([[]], errors=["inbox: permission denied"])

    for _ in range(ctx.settings.ingest_max_backfill_attempts):
        ingest_job.run_backfill(session, src, impl, ctx)

    assert src.backfill_stuck and src.backfill_attempts >= 20
    assert "permission denied" in (src.backfill_last_error or "")

    from bidtriage.worker.digest_job import health_lines

    line = health_lines(session, NOW)[0]
    assert "backfill stuck" in line.detail and line.status in ("degraded", "down")
    # Stuck means stop trying, not retry forever.
    before = src.backfill_attempts
    ingest_job.run_backfill(session, src, impl, ctx)
    assert src.backfill_attempts == before


def test_seed_failure_finalises_poll_row(session, ctx):
    """A failing seed must not leave a dangling poll row (SPEC-10 F5)."""

    class BadSeed(StubSource):
        def seed(self, state: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("network unreachable")

    src = _source(session)
    src.delta_state = {}
    src.backfill_days = 90
    session.flush()
    with pytest.raises(RuntimeError):
        ingest_job.run_poll(session, src, BadSeed([[]]), ctx)

    poll = session.scalars(select(SourcePoll)).one()
    assert poll.finished_at is not None
    assert "network unreachable" in poll.errors[0]
    assert src.status == "error" and src.last_success_at is None


# ---------------------------------------------------------------- SPEC-10 F6


def test_abandoned_poll_row_is_closed(session, ctx):
    src = _source(session)
    session.add(
        SourcePoll(
            id="dangling", source_id=src.id, mode="live", started_at=NOW - timedelta(hours=2)
        )
    )
    fresh = SourcePoll(id="fresh", source_id=src.id, mode="live", started_at=NOW)
    session.add(fresh)
    session.flush()

    assert ingest_job.close_abandoned_polls(session, now=NOW) == 1
    closed = session.get(SourcePoll, "dangling")
    assert closed is not None and closed.finished_at is not None
    assert "abandoned" in closed.errors[0] and closed.error_count == 1
    assert session.get(SourcePoll, "fresh").finished_at is None, "a running poll is left alone"


def test_old_polls_are_pruned(session, ctx):
    src = _source(session)
    for i, age in enumerate([1, 10, 45, 90]):
        session.add(
            SourcePoll(
                id=f"p{i}",
                source_id=src.id,
                mode="live",
                started_at=NOW - timedelta(days=age),
                finished_at=NOW - timedelta(days=age),
            )
        )
    session.flush()
    assert ingest_job.prune_polls(session, now=NOW, retention_days=30) == 2
    assert session.scalar(select(func.count()).select_from(SourcePoll)) == 2


def test_latest_poll_uses_a_limit(session, ctx):
    src = _source(session)
    for i in range(3):
        session.add(
            SourcePoll(
                id=f"p{i}",
                source_id=src.id,
                mode="live",
                started_at=NOW - timedelta(minutes=i * 5),
                finished_at=NOW,
                seen=i,
            )
        )
    session.flush()
    latest = ingest_job.latest_poll(session, src.id)
    assert latest is not None and latest.id == "p0"


# ---------------------------------------------------------------- SPEC-10 F7


def test_transport_receipt_time_wins(session, ctx):
    """Graph/IMAP receipt time beats the message's own Received: header (SPEC-10 F7)."""
    transport = datetime(2026, 9, 29, 20, 45, tzinfo=UTC)
    trace = datetime(2019, 1, 1, tzinfo=UTC)
    got = pipeline.resolve_received_at(
        transport=transport, trace=trace, now=NOW, floor=NOW - timedelta(days=90)
    )
    assert got == transport


def test_received_at_is_clamped(session, ctx):
    """A forged Received: header cannot push a message outside the believable window."""
    floor = NOW - timedelta(days=90)
    ancient = pipeline.resolve_received_at(
        transport=None, trace=datetime(2019, 1, 1, tzinfo=UTC), now=NOW, floor=floor
    )
    assert ancient == floor
    future = pipeline.resolve_received_at(
        transport=None, trace=NOW + timedelta(days=365), now=NOW, floor=floor
    )
    assert future == NOW
    assert pipeline.resolve_received_at(transport=None, trace=None, now=NOW, floor=floor) == NOW


def test_forged_received_header_cannot_escape_the_dedupe_window(session, ctx):
    """The clamp matters because received_at drives the 7-day dedupe window (SPEC-10 F7)."""
    src = _source(session)
    raw = (
        b"Received: from evil by ferry.mail with SMTP id X; Tue, 1 Jan 2019 00:00:00 -0500\r\n"
        b"Message-ID: <forged@gc.com>\r\nFrom: gc@example-gc.com\r\nSubject: ITB\r\n"
        b"Date: Tue, 29 Sep 2026 16:45:00 -0400\r\n\r\nBids due 10/20.\r\n"
    )
    ingest_job.run_poll(session, src, StubSource([[("p1", raw)]]), ctx)
    msg = session.scalars(select(RawMessage)).one()
    from bidtriage.core.clock import aware

    received = aware(msg.received_at)
    assert received is not None and received.year >= 2026


# ---------------------------------------------------------------- SPEC-10 F8


def test_ingestion_metrics_at_volume(session, ctx):
    """Aggregates only: the row-by-row version took seconds and hundreds of MB here (SPEC-10 F8)."""

    src = _source(session)
    body = "x" * 3000
    for i in range(3000):
        session.add(
            RawMessage(
                id=f"m{i}",
                content_hash=f"h{i}",
                subject="Newsletter",
                body_text=body,
                body_html=body,
                headers={},
                received_at=NOW - timedelta(seconds=30),
                created_at=NOW,
            )
        )
        session.add(
            RawAttachment(
                id=f"a{i}", message_id=f"m{i}", filename="x.pdf", sha256=f"{i:064d}", text="t"
            )
        )
    session.add(
        SourcePoll(
            source_id=src.id,
            mode="live",
            started_at=NOW,
            finished_at=NOW,
            seen=3000,
            new=3000,
            error_count=0,
        )
    )
    session.commit()
    session.expunge_all()

    started = time.perf_counter()
    m = ingest_job.ingestion_metrics(session, now=NOW + timedelta(minutes=1))
    elapsed = time.perf_counter() - started

    assert m.messages_new == 3000 and m.attachments_extracted == 3000
    assert m.lag_p50_seconds is not None and 25 <= m.lag_p50_seconds <= 120
    # Nothing was hydrated into the identity map: no bodies were selected.
    assert not any(isinstance(o, RawMessage) for o in session.identity_map.values())
    assert elapsed < 1.0, f"metrics took {elapsed:.2f}s"


def test_ingestion_metrics_counts_clock_skew(session, ctx):
    session.add(
        RawMessage(
            id="skewed",
            content_hash="h",
            headers={},
            received_at=NOW + timedelta(minutes=5),
            created_at=NOW,
        )
    )
    session.flush()
    m = ingest_job.ingestion_metrics(session, now=NOW + timedelta(minutes=1))
    assert m.clock_skew == 1


def test_ingestion_metrics_cache(session, ctx):
    src = _source(session)
    ingest_job.run_poll(session, src, StubSource([[("p1", _eml("a@gc.com"))]]), ctx)
    first = ingest_job.ingestion_metrics(session, now=NOW, cache_seconds=60)
    ingest_job.run_poll(session, src, StubSource([[("p2", _eml("b@gc.com"))]]), ctx)
    cached = ingest_job.ingestion_metrics(session, now=NOW, cache_seconds=60)
    assert cached is first, "repeated admin refreshes must not recompute"
    fresh = ingest_job.ingestion_metrics(session, now=NOW)
    assert fresh.polls == 2


# ---------------------------------------------------------------- SPEC-10 F10


def test_alert_is_per_recipient(session, ctx):
    """One bad address must not cause the whole alert to be re-sent to everyone (SPEC-10 F10)."""
    session.add_all(
        [
            User(email="good@ferryelectric.com", name="Good", role="admin"),
            User(email="bounce@ferryelectric.com", name="Bounce", role="chief"),
        ]
    )
    src = _source(session)
    src.last_success_at = NOW - timedelta(minutes=90)
    session.flush()

    sent: list[str] = []

    def mailer(*, to: str, subject: str, html: str, text: str) -> str:
        if to.startswith("bounce"):
            raise RuntimeError("550 mailbox unavailable")
        sent.append(to)
        return "<id>"

    ingest_job.check_sources(session, ctx, now=NOW, mailer=mailer)
    assert sent == ["good@ferryelectric.com"]
    assert src.down_alert_sent_at is not None, "delivery to one admin is enough to arm the alert"

    ingest_job.check_sources(session, ctx, now=NOW, mailer=mailer)
    assert sent == ["good@ferryelectric.com"], "not re-sent on the next sweep"


def test_alert_not_armed_when_nothing_could_be_delivered(session, ctx):
    session.add(User(email="admin@ferryelectric.com", name="Admin", role="admin"))
    src = _source(session)
    src.last_success_at = NOW - timedelta(minutes=90)
    session.flush()

    def broken(*, to: str, subject: str, html: str, text: str) -> str:
        raise RuntimeError("smtp down")

    ingest_job.check_sources(session, ctx, now=NOW, mailer=broken)
    assert src.down_alert_sent_at is None, "retry the alert once SMTP recovers"
