"""Source transports: cursor safety, streaming, read-only guarantees, backfill (SPEC-01, SPEC-10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bidtriage.ingestion.file_source import FileSource
from bidtriage.ingestion.graph_source import GRAPH, GraphSource
from bidtriage.ingestion.health import DEGRADED, DOWN, OK, PAUSED, source_health
from bidtriage.ingestion.imap_source import ImapSource
from bidtriage.ingestion.protocol import FetchOptions
from tests.helpers import drain

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=90)


def _eml(mid: str, subject: str = "ITB") -> bytes:
    return (
        f"Message-ID: <{mid}>\r\nFrom: gc@example-gc.com\r\nTo: estimating@ferryelectric.com\r\n"
        f"Subject: {subject}\r\nDate: Tue, 29 Sep 2026 16:45:00 -0400\r\n\r\nBids due 10/20.\r\n"
    ).encode()


def _opts(**kw: Any) -> FetchOptions:
    return FetchOptions(**kw)


# ---------------------------------------------------------------- Graph


class FakeGraph:
    """Records every URL requested so tests can assert nothing but reads happened."""

    def __init__(self, pages: dict[str, dict[str, Any]], *, expire: set[str] | None = None) -> None:
        self.pages = pages
        self.expire = expire or set()
        self.requested: list[str] = []
        self.fetched: list[str] = []
        self.fetch_fails: set[str] = set()

    def transport(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self.requested.append(url)
        if url in self.expire:
            from bidtriage.ingestion.graph_source import DeltaExpiredError

            self.expire.discard(url)
            raise DeltaExpiredError()
        return self.pages[url]

    def mime(self, message_id: str) -> bytes:
        if message_id in self.fetch_fails:
            raise RuntimeError("503 throttled")
        self.fetched.append(message_id)
        return _eml(f"{message_id}@gc.com")

    def source(self) -> GraphSource:
        return GraphSource(
            "tenant",
            "client",
            "secret",
            "estimating@ferryelectric.com",
            transport=self.transport,
            mime_fetch=self.mime,
        )


def _delta() -> str:
    return f"{GRAPH}/users/estimating@ferryelectric.com/mailFolders/inbox/messages/delta"


def test_graph_poll_stores_delta_link():
    d = _delta()
    fake = FakeGraph(
        {
            d: {
                "value": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                "@odata.deltaLink": f"{d}?$deltatoken=abc",
            }
        }
    )
    src = fake.source()
    stored, state = drain(src.fetch({}, _opts()))
    assert stored == ["m1", "m2", "m3"]
    assert state["delta:inbox"] == f"{d}?$deltatoken=abc"
    # Every Graph call is a read: no PATCH of isRead, no move, no delete.
    assert all(url.startswith(GRAPH) for url in fake.requested)


def test_graph_cursor_holds_on_fetch_failure():
    """SPEC-10 F1: a message whose MIME fetch fails must stay in front of the cursor."""
    d = _delta()
    fake = FakeGraph(
        {d: {"value": [{"id": "m1"}, {"id": "m2"}], "@odata.deltaLink": f"{d}?$deltatoken=abc"}}
    )
    fake.fetch_fails = {"m1"}
    src = fake.source()
    session = src.fetch({}, _opts())
    stored, state = drain(session)

    assert stored == ["m2"]
    assert "m1" in session.failures
    assert "delta:inbox" not in state, "the delta link must not advance past the unfetched message"

    fake.fetch_fails.clear()
    stored_again, state_again = drain(src.fetch(state, _opts()))
    assert "m1" in stored_again, "the message is recovered on the next poll"
    assert state_again["delta:inbox"] == f"{d}?$deltatoken=abc"


def test_graph_cursor_holds_on_store_failure():
    d = _delta()
    fake = FakeGraph(
        {d: {"value": [{"id": "m1"}, {"id": "m2"}], "@odata.deltaLink": f"{d}?$deltatoken=abc"}}
    )
    _, state = drain(fake.source().fetch({}, _opts()), fails={"m2"})
    assert "delta:inbox" not in state


def test_graph_cursor_advances_over_given_up_message():
    """Once ingestion gives up on a message the cursor may pass it (SPEC-10 F1)."""
    d = _delta()
    fake = FakeGraph(
        {d: {"value": [{"id": "m1"}, {"id": "m2"}], "@odata.deltaLink": f"{d}?$deltatoken=abc"}}
    )
    fake.fetch_fails = {"m1"}
    stored, state = drain(fake.source().fetch({}, _opts(skip_ids=frozenset({"m1"}))))
    assert stored == ["m2"]
    assert state["delta:inbox"] == f"{d}?$deltatoken=abc"


def test_graph_one_stuck_folder_does_not_freeze_the_others():
    base = f"{GRAPH}/users/mbx/mailFolders"
    pages = {
        f"{base}/inbox/messages/delta": {
            "value": [{"id": "bad"}],
            "@odata.deltaLink": "inbox-token",
        },
        f"{base}/archive/messages/delta": {
            "value": [{"id": "good"}],
            "@odata.deltaLink": "archive-token",
        },
    }
    fake = FakeGraph(pages)
    fake.fetch_fails = {"bad"}
    src = GraphSource(
        "t", "c", "s", "mbx", ["inbox", "archive"], transport=fake.transport, mime_fetch=fake.mime
    )
    stored, state = drain(src.fetch({}, _opts()))
    assert stored == ["good"]
    assert "delta:inbox" not in state and state["delta:archive"] == "archive-token"


def test_delta_token_reset():
    """A 410 Gone drops the stored token and resyncs the folder without creating duplicates."""
    d = _delta()
    stale = f"{d}?$deltatoken=stale"
    fake = FakeGraph(
        {d: {"value": [{"id": "m1"}], "@odata.deltaLink": f"{d}?$deltatoken=fresh"}},
        expire={stale},
    )
    session = fake.source().fetch({"delta:inbox": stale}, _opts())
    stored, state = drain(session)
    assert any("delta token expired" in e for e in session.errors)
    assert stored == ["m1"] and state["delta:inbox"] == f"{d}?$deltatoken=fresh"


def test_delta_token_reset_does_not_write_back_the_expired_token():
    d = _delta()
    stale = f"{d}?$deltatoken=stale"
    fake = FakeGraph(
        {d: {"value": [{"id": "m1"}], "@odata.deltaLink": f"{d}?$deltatoken=fresh"}}, expire={stale}
    )
    fake.fetch_fails = {"m1"}
    _, state = drain(fake.source().fetch({"delta:inbox": stale}, _opts()))
    assert "delta:inbox" not in state, "an expired token must never be persisted again"


def test_graph_skips_removed_items():
    d = _delta()
    fake = FakeGraph(
        {
            d: {
                "value": [{"id": "m1"}, {"id": "gone", "@removed": {"reason": "deleted"}}],
                "@odata.deltaLink": f"{d}?$deltatoken=abc",
            }
        }
    )
    stored, _ = drain(fake.source().fetch({}, _opts()))
    assert stored == ["m1"]


def test_graph_records_transport_receipt_time():
    """SPEC-10 F7: Graph's receivedDateTime beats the sender-controlled Received: header."""
    d = _delta()
    fake = FakeGraph(
        {
            d: {
                "value": [{"id": "m1", "receivedDateTime": "2026-09-29T20:45:12Z"}],
                "@odata.deltaLink": f"{d}?$deltatoken=abc",
            }
        }
    )
    items = list(fake.source().fetch({}, _opts()))
    assert items[0].received_at == datetime(2026, 9, 29, 20, 45, 12, tzinfo=UTC)


def test_graph_seed_takes_a_token_without_fetching_messages():
    d = _delta()
    fake = FakeGraph({f"{d}?$deltatoken=latest": {"@odata.deltaLink": f"{d}?$deltatoken=now"}})
    assert fake.source().seed({}) == {"delta:inbox": f"{d}?$deltatoken=now"}
    assert fake.fetched == []


def test_graph_backfill_pages_oldest_first():
    base = f"{GRAPH}/users/estimating@ferryelectric.com/mailFolders/inbox/messages"
    page2 = f"{base}?$skiptoken=p2"
    fake = FakeGraph(
        {
            base: {"value": [{"id": "old1"}, {"id": "old2"}], "@odata.nextLink": page2},
            page2: {"value": [{"id": "old3"}]},
        }
    )
    src = fake.source()
    stored, state = drain(src.fetch({}, _opts(limit=2, since=SINCE)))
    assert stored == ["old1", "old2"] and state["backfill:inbox"] == page2
    stored2, state2 = drain(src.fetch(state, _opts(limit=2, since=SINCE)))
    assert stored2 == ["old3"] and state2["backfill:inbox"] == "done"


def test_graph_token_refresh(monkeypatch):
    """SPEC-10 F3: the cached token is refreshed from expires_in, not kept forever."""
    calls: list[str] = []

    class Resp:
        status_code = 200

        def __init__(self, payload):  # type: ignore[no-untyped-def]
            self._payload = payload

        def json(self):  # type: ignore[no-untyped-def]
            return self._payload

        def raise_for_status(self):  # type: ignore[no-untyped-def]
            return None

    def fake_post(url, **kw):  # type: ignore[no-untyped-def]
        calls.append("token")
        return Resp({"access_token": f"tok{len(calls)}", "expires_in": 3600})

    monkeypatch.setattr("bidtriage.ingestion.graph_source.httpx.post", fake_post)
    src = GraphSource("t", "c", "s", "mbx")
    assert src._auth() == "tok1"
    assert src._auth() == "tok1", "still valid, so no second token request"
    assert calls == ["token"]

    # Simulate the token nearing expiry: the next call must refresh.
    src._token_expires_at = datetime.now(tz=UTC) + timedelta(seconds=30)
    assert src._auth() == "tok2"
    assert len(calls) == 2


# ---------------------------------------------------------------- IMAP


class FakeImap:
    def __init__(self, messages: dict[int, bytes], uidvalidity: str = "100") -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.selects: list[tuple[str, bool]] = []
        self.fetches: list[tuple[str, str]] = []
        self.searches: list[str] = []
        self.fetch_fails: set[int] = set()
        self.internaldate = "Tue, 29 Sep 2026 16:45:12 -0400"

    def login(self, user: str, password: str) -> Any:
        return ("OK", [b""])

    def select(self, mailbox: str, readonly: bool = False) -> Any:
        self.selects.append((mailbox, readonly))
        return ("OK", [str(len(self.messages)).encode()])

    def response(self, which: str) -> Any:
        return (which, [self.uidvalidity.encode()])

    def uid(self, command: str, *args: str) -> Any:
        if command == "search":
            self.searches.append(args[0])
            low = int(args[0].split()[1].split(":")[0])
            hits = sorted(u for u in self.messages if u >= low)
            return ("OK", [" ".join(str(u) for u in hits).encode()])
        self.fetches.append((args[0], args[1]))
        uid = int(args[0])
        if uid in self.fetch_fails:
            return ("NO", [None])
        header = f'{uid} (UID {uid} INTERNALDATE "{self.internaldate}" BODY[] {{x}})'.encode()
        return ("OK", [(header, self.messages[uid])])

    def logout(self) -> Any:
        return ("BYE", [b""])


def _imap(fake: FakeImap) -> ImapSource:
    return ImapSource("mail.example.com", 993, "u", "p", connect=lambda: fake)


def test_imap_is_read_only_and_tracks_last_uid():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")})
    stored, state = drain(_imap(fake).fetch({}, _opts()))
    assert stored == ["INBOX:100:1", "INBOX:100:2"]
    assert state["INBOX"] == {"uidvalidity": "100", "last_uid": 2}
    # EXAMINE, not SELECT, and BODY.PEEK so \\Seen never changes.
    assert fake.selects == [("INBOX", True)]
    assert {cmd for _, cmd in fake.fetches} == {"(BODY.PEEK[] INTERNALDATE)"}

    stored_again, _ = drain(_imap(fake).fetch(state, _opts()))
    assert stored_again == [] and fake.searches[-1] == "UID 3:*"


def test_imap_uid_holds_on_fetch_failure():
    """SPEC-10 F1: a transient FETCH failure must not lose the message.

    Replaces the earlier test that asserted the opposite: `last_uid` stops *below* the first
    failure, so the message is retried once the failure clears.
    """
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com"), 3: _eml("c@gc.com")})
    fake.fetch_fails = {1}
    session = _imap(fake).fetch({}, _opts())
    stored, state = drain(session)

    assert stored == ["INBOX:100:2", "INBOX:100:3"]
    assert state["INBOX"]["last_uid"] == 0, "the cursor stays below the failed UID"
    assert "INBOX:100:1" in session.failures

    fake.fetch_fails.clear()
    stored_again, state_again = drain(_imap(fake).fetch(state, _opts()))
    assert "INBOX:100:1" in stored_again
    assert state_again["INBOX"]["last_uid"] == 3


def test_imap_uid_stops_below_a_middle_failure():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com"), 3: _eml("c@gc.com")})
    fake.fetch_fails = {2}
    _, state = drain(_imap(fake).fetch({}, _opts()))
    assert state["INBOX"]["last_uid"] == 1


def test_imap_uid_advances_over_given_up_message():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")})
    fake.fetch_fails = {1}
    _, state = drain(_imap(fake).fetch({}, _opts(skip_ids=frozenset({"INBOX:100:1"}))))
    assert state["INBOX"]["last_uid"] == 2


def test_uidvalidity_change():
    """A new UIDVALIDITY invalidates stored UIDs, so the folder is rescanned from UID 1."""
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")}, uidvalidity="200")
    stored, state = drain(
        _imap(fake).fetch({"INBOX": {"uidvalidity": "100", "last_uid": 2}}, _opts())
    )
    assert fake.searches == ["UID 1:*"]
    assert stored == ["INBOX:200:1", "INBOX:200:2"]
    assert state["INBOX"]["uidvalidity"] == "200"


def test_imap_records_internaldate():
    """SPEC-10 F7: INTERNALDATE is the server's own receipt time and cannot be forged."""
    fake = FakeImap({1: _eml("a@gc.com")})
    items = list(_imap(fake).fetch({}, _opts()))
    assert items[0].received_at == datetime(2026, 9, 29, 20, 45, 12, tzinfo=UTC)


def test_imap_seed_records_the_current_highest_uid():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com"), 7: _eml("c@gc.com")})
    state = _imap(fake).seed({})
    assert state == {"INBOX": {"uidvalidity": "100", "last_uid": 7}}
    assert fake.fetches == []
    assert drain(_imap(fake).fetch(state, _opts()))[0] == []


def test_imap_backfill_uses_since_and_separate_cursor():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com"), 3: _eml("c@gc.com")})
    src = _imap(fake)
    stored, state = drain(src.fetch({}, _opts(limit=2, since=SINCE)))
    assert fake.searches[-1].endswith("SINCE 02-Jul-2026")
    assert len(stored) == 2 and state["backfill:INBOX"]["last_uid"] == 2
    assert "done" not in state["backfill:INBOX"]
    stored2, state2 = drain(src.fetch(state, _opts(limit=2, since=SINCE)))
    assert len(stored2) == 1 and state2["backfill:INBOX"]["done"] is True


def test_imap_backfill_not_marked_done_while_a_message_is_outstanding():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")})
    fake.fetch_fails = {2}
    _, state = drain(_imap(fake).fetch({}, _opts(limit=10, since=SINCE)))
    assert "done" not in state["backfill:INBOX"]


# ---------------------------------------------------------------- streaming (F2)


def test_fetch_is_lazy():
    """SPEC-10 F2: bodies are fetched as they are consumed, not all up front."""
    d = _delta()
    fake = FakeGraph(
        {
            d: {
                "value": [{"id": f"m{i}"} for i in range(5)],
                "@odata.deltaLink": f"{d}?$deltatoken=abc",
            }
        }
    )
    iterator = iter(fake.source().fetch({}, _opts()))
    next(iterator)
    assert len(fake.fetched) == 1, "only the consumed message was downloaded"
    next(iterator)
    assert len(fake.fetched) == 2


def test_batch_byte_cap_stops_poll_early():
    import base64

    big = base64.b64encode(b"%PDF-1.4" + b"\0" * (2 * 1024 * 1024)).decode()
    raw = (
        "Message-ID: <big@gc.com>\r\nFrom: gc@x.com\r\nSubject: ITB\r\n"
        "Date: Tue, 29 Sep 2026 16:45:00 -0400\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nx\r\n'
        '--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="s.pdf"\r\n'
        f"Content-Transfer-Encoding: base64\r\n\r\n{big}\r\n--B--\r\n"
    ).encode()
    fake = FakeImap({1: raw, 2: raw, 3: raw})
    session = _imap(fake).fetch({}, _opts(max_bytes=5 * 1024 * 1024))
    stored, state = drain(session)
    assert len(stored) == 2, "stopped once the attachment budget was spent"
    assert session.more_available
    assert state["INBOX"]["last_uid"] == 2, "the unread message stays in front of the cursor"


def test_oversized_single_message_still_ingested():
    """A message bigger than the whole budget is processed alone, never wedged (SPEC-10 F2)."""
    import base64

    big = base64.b64encode(b"%PDF-1.4" + b"\0" * (4 * 1024 * 1024)).decode()
    raw = (
        "Message-ID: <huge@gc.com>\r\nFrom: gc@x.com\r\nSubject: ITB\r\n"
        "Date: Tue, 29 Sep 2026 16:45:00 -0400\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nx\r\n'
        '--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="s.pdf"\r\n'
        f"Content-Transfer-Encoding: base64\r\n\r\n{big}\r\n--B--\r\n"
    ).encode()
    fake = FakeImap({1: raw})
    stored, _ = drain(_imap(fake).fetch({}, _opts(max_bytes=1024)))
    assert stored == ["INBOX:100:1"]


def test_limit_stops_the_batch_and_reports_more():
    fake = FakeImap({i: _eml(f"m{i}@gc.com") for i in range(1, 6)})
    session = _imap(fake).fetch({}, _opts(limit=2))
    stored, state = drain(session)
    assert len(stored) == 2 and session.more_available
    assert state["INBOX"]["last_uid"] == 2


# ---------------------------------------------------------------- file source


def test_file_source_is_idempotent_and_limits(tmp_path):
    for i in range(3):
        (tmp_path / f"m{i}.eml").write_bytes(_eml(f"m{i}@gc.com"))
    src = FileSource(tmp_path)
    stored, state = drain(src.fetch({}, _opts(limit=2)))
    assert len(stored) == 2
    stored2, state2 = drain(src.fetch(state, _opts(limit=2)))
    assert len(stored2) == 1
    assert drain(src.fetch(state2, _opts()))[0] == []


def test_file_source_retries_an_unparseable_file(tmp_path):
    """SPEC-10 F1: a file that failed to parse is not marked seen, so it is retried."""
    (tmp_path / "broken.msg").write_bytes(b"\xd0\xcf\x11\xe0not really a compound file")
    session = FileSource(tmp_path).fetch({}, _opts())
    stored, state = drain(session)
    assert stored == [] and len(session.failures) == 1
    assert state["seen"] == [], "not remembered, so the next poll tries again"
    # ...until ingestion gives up on it, after which it is passed over.
    _, state2 = drain(FileSource(tmp_path).fetch({}, _opts(skip_ids=frozenset({"broken.msg"}))))
    assert state2["seen"] == ["broken.msg"]


def test_file_source_cannot_backfill():
    """Pointing at a directory should ingest it, so FileSource opts out of seeding (SPEC-10 F5)."""
    from bidtriage.ingestion.protocol import supports_backfill

    assert not supports_backfill(FileSource(__import__("pathlib").Path(".")))


# ---------------------------------------------------------------- health thresholds


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, OK), (29, OK), (31, DEGRADED), (59, DEGRADED), (61, DOWN), (600, DOWN)],
)
def test_source_health_thresholds(minutes, expected):
    assert (
        source_health(last_success_at=NOW - timedelta(minutes=minutes), now=NOW).status == expected
    )


def test_source_health_never_polled_and_paused():
    assert source_health(last_success_at=None, now=NOW).status == DOWN
    assert source_health(last_success_at=None, now=NOW, paused=True).status == PAUSED
