"""Source transports: delta/UID state, read-only guarantees and backfill (SPEC-01 F1, F7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bidtriage.ingestion.file_source import FileSource
from bidtriage.ingestion.graph_source import GRAPH, GraphSource
from bidtriage.ingestion.health import DEGRADED, DOWN, OK, PAUSED, source_health
from bidtriage.ingestion.imap_source import ImapSource

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _eml(mid: str, subject: str = "ITB") -> bytes:
    return (
        f"Message-ID: <{mid}>\r\nFrom: gc@example-gc.com\r\nTo: estimating@ferryelectric.com\r\n"
        f"Subject: {subject}\r\nDate: Tue, 29 Sep 2026 16:45:00 -0400\r\n\r\nBids due 10/20.\r\n"
    ).encode()


# ---------------------------------------------------------------- Graph


class FakeGraph:
    """Records every URL requested so tests can assert nothing but reads happened."""

    def __init__(self, pages: dict[str, dict[str, Any]], *, expire: set[str] | None = None) -> None:
        self.pages = pages
        self.expire = expire or set()
        self.requested: list[str] = []
        self.fetched: list[str] = []

    def transport(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self.requested.append(url)
        if url in self.expire:
            from bidtriage.ingestion.graph_source import DeltaExpiredError

            self.expire.discard(url)
            raise DeltaExpiredError()
        return self.pages[url]

    def mime(self, message_id: str) -> bytes:
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


def test_graph_poll_stores_delta_link():
    delta = f"{GRAPH}/users/estimating@ferryelectric.com/mailFolders/inbox/messages/delta"
    fake = FakeGraph(
        {
            delta: {
                "value": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                "@odata.deltaLink": f"{delta}?$deltatoken=abc",
            }
        }
    )
    res = fake.source().poll({})
    assert [pid for pid, _ in res.messages] == ["m1", "m2", "m3"]
    assert res.new_state["delta:inbox"] == f"{delta}?$deltatoken=abc"
    assert res.errors == []
    # Every Graph call is a read: no PATCH of isRead, no move, no delete.
    assert all(url.startswith(GRAPH) for url in fake.requested)
    assert fake.fetched == ["m1", "m2", "m3"]


def test_delta_token_reset():
    """A 410 Gone drops the stored token and resyncs the folder without creating duplicates."""
    delta = f"{GRAPH}/users/estimating@ferryelectric.com/mailFolders/inbox/messages/delta"
    stale = f"{delta}?$deltatoken=stale"
    fake = FakeGraph(
        {
            delta: {"value": [{"id": "m1"}], "@odata.deltaLink": f"{delta}?$deltatoken=fresh"},
        },
        expire={stale},
    )
    res = fake.source().poll({"delta:inbox": stale})
    assert any("delta token expired" in e for e in res.errors)
    assert res.new_state["delta:inbox"] == f"{delta}?$deltatoken=fresh"
    assert [pid for pid, _ in res.messages] == ["m1"]


def test_graph_skips_removed_items_and_records_per_message_errors():
    delta = f"{GRAPH}/users/estimating@ferryelectric.com/mailFolders/inbox/messages/delta"
    fake = FakeGraph(
        {
            delta: {
                "value": [{"id": "m1"}, {"id": "gone", "@removed": {"reason": "deleted"}}],
                "@odata.deltaLink": f"{delta}?$deltatoken=abc",
            }
        }
    )

    def boom(message_id: str) -> bytes:
        if message_id == "m1":
            raise RuntimeError("mailbox throttled")
        return _eml(message_id)

    src = fake.source()
    src._mime_fetch = boom
    res = src.poll({})
    assert res.messages == [] and res.errors == ["m1: mailbox throttled"]


def test_graph_seed_takes_a_token_without_fetching_messages():
    delta = f"{GRAPH}/users/estimating@ferryelectric.com/mailFolders/inbox/messages/delta"
    fake = FakeGraph(
        {f"{delta}?$deltatoken=latest": {"@odata.deltaLink": f"{delta}?$deltatoken=now"}}
    )
    state = fake.source().seed({})
    assert state == {"delta:inbox": f"{delta}?$deltatoken=now"}
    assert fake.fetched == []  # seeding must not download the folder


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
    first = src.backfill({}, since=NOW - timedelta(days=90), limit=2)
    assert [pid for pid, _ in first.messages] == ["old1", "old2"]
    assert first.more_available and first.new_state["backfill:inbox"] == page2
    second = src.backfill(first.new_state, since=NOW - timedelta(days=90), limit=2)
    assert [pid for pid, _ in second.messages] == ["old3"]
    assert not second.more_available and second.new_state["backfill:inbox"] == "done"


# ---------------------------------------------------------------- IMAP


class FakeImap:
    def __init__(self, messages: dict[int, bytes], uidvalidity: str = "100") -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.selects: list[tuple[str, bool]] = []
        self.fetches: list[tuple[str, str]] = []
        self.searches: list[str] = []

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
        return ("OK", [(f"{uid} (UID {uid} BODY[] {{x}})".encode(), self.messages[uid])])

    def logout(self) -> Any:
        return ("BYE", [b""])


def _imap(fake: FakeImap) -> ImapSource:
    return ImapSource("mail.example.com", 993, "u", "p", connect=lambda: fake)


def test_imap_is_read_only_and_tracks_last_uid():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")})
    res = _imap(fake).poll({})
    assert [pid for pid, _ in res.messages] == ["INBOX:100:1", "INBOX:100:2"]
    assert res.new_state["INBOX"] == {"uidvalidity": "100", "last_uid": 2}
    # EXAMINE, not SELECT, and BODY.PEEK so \Seen never changes.
    assert fake.selects == [("INBOX", True)]
    assert {cmd for _, cmd in fake.fetches} == {"(BODY.PEEK[])"}

    again = _imap(fake).poll(res.new_state)
    assert again.messages == [] and fake.searches[-1] == "UID 3:*"


def test_uidvalidity_change():
    """A new UIDVALIDITY invalidates stored UIDs, so the folder is rescanned from UID 1."""
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")}, uidvalidity="200")
    res = _imap(fake).poll({"INBOX": {"uidvalidity": "100", "last_uid": 2}})
    assert fake.searches == ["UID 1:*"]
    assert [pid for pid, _ in res.messages] == ["INBOX:200:1", "INBOX:200:2"]
    assert res.new_state["INBOX"]["uidvalidity"] == "200"


def test_imap_fetch_failure_does_not_advance_uid():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com")})

    def bad_uid(command: str, *args: str) -> Any:
        if command == "fetch" and args[0] == "1":
            return ("NO", [None])
        return FakeImap.uid(fake, command, *args)

    fake.uid = bad_uid  # type: ignore[method-assign]
    res = _imap(fake).poll({})
    assert [pid for pid, _ in res.messages] == ["INBOX:100:2"]
    assert res.errors == ["INBOX:1: fetch failed"]


def test_imap_seed_records_the_current_highest_uid():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com"), 7: _eml("c@gc.com")})
    state = _imap(fake).seed({})
    assert state == {"INBOX": {"uidvalidity": "100", "last_uid": 7}}
    assert fake.fetches == []  # seeding must not download the folder
    assert _imap(fake).poll(state).messages == []


def test_imap_backfill_uses_since_and_separate_cursor():
    fake = FakeImap({1: _eml("a@gc.com"), 2: _eml("b@gc.com"), 3: _eml("c@gc.com")})
    res = _imap(fake).backfill({}, since=NOW - timedelta(days=90), limit=2)
    assert fake.searches[-1].endswith("SINCE 02-Jul-2026")
    assert len(res.messages) == 2 and res.more_available
    assert res.new_state["backfill:INBOX"] == {"uidvalidity": "100", "last_uid": 2}
    final = _imap(fake).backfill(res.new_state, since=NOW - timedelta(days=90), limit=2)
    assert len(final.messages) == 1 and not final.more_available
    assert final.new_state["backfill:INBOX"]["done"] is True


# ---------------------------------------------------------------- file source


def test_file_source_is_idempotent_and_limits(tmp_path):
    for i in range(3):
        (tmp_path / f"m{i}.eml").write_bytes(_eml(f"m{i}@gc.com"))
    src = FileSource(tmp_path)
    first = src.poll({}, limit=2)
    assert len(first.messages) == 2 and first.more_available
    second = src.poll(first.new_state, limit=2)
    assert len(second.messages) == 1 and not second.more_available
    assert src.poll(second.new_state).messages == []


def test_file_source_records_unparseable_file(tmp_path):
    (tmp_path / "broken.msg").write_bytes(b"\xd0\xcf\x11\xe0not really a compound file")
    res = FileSource(tmp_path).poll({})
    assert res.messages == [] and len(res.errors) == 1
    assert FileSource(tmp_path).poll(res.new_state).errors == []  # not retried forever


# ---------------------------------------------------------------- health thresholds


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, OK), (29, OK), (31, DEGRADED), (59, DEGRADED), (61, DOWN), (600, DOWN)],
)
def test_source_health_thresholds(minutes, expected):
    health = source_health(last_success_at=NOW - timedelta(minutes=minutes), now=NOW)
    assert health.status == expected


def test_source_health_never_polled_and_paused():
    assert source_health(last_success_at=None, now=NOW).status == DOWN
    assert source_health(last_success_at=None, now=NOW, paused=True).status == PAUSED
