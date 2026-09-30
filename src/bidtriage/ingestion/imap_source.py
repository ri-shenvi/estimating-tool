"""IMAP UID-poll source (ADR-003 fallback, SPEC-01 F1/F7, SPEC-10 F1/F2/F7).

Read-only by construction: the folder is opened with `readonly=True` (EXAMINE, not SELECT) and
bodies are fetched with `BODY.PEEK[]`, so neither \\Seen nor any other flag changes. That is the
SPEC-01 "never modify source mail" guarantee for this transport.

`last_uid` only advances over UIDs that were durably stored, so a transient `FETCH` failure leaves
the message in front of the cursor instead of losing it (SPEC-10 F1).
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from bidtriage.ingestion.eml import parse_eml
from bidtriage.ingestion.protocol import CheckpointSession, FetchedMessage, FetchOptions

_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"', re.I)


class ImapConnection(Protocol):
    def login(self, user: str, password: str) -> Any: ...
    def select(self, mailbox: str, readonly: bool = ...) -> Any: ...
    def response(self, which: str) -> Any: ...
    def uid(self, command: str, *args: str) -> Any: ...
    def logout(self) -> Any: ...


class ImapSource:
    can_backfill = True

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        folders: list[str] | None = None,
        tls: bool = True,
        *,
        connect: Callable[[], ImapConnection] | None = None,
    ) -> None:
        self.host, self.port, self.user, self.password, self.tls = host, port, user, password, tls
        self.folders = folders or ["INBOX"]
        self._connect = connect

    def open(self) -> ImapConnection:
        if self._connect is not None:
            conn = self._connect()
        else:
            conn = (
                imaplib.IMAP4_SSL(self.host, self.port)
                if self.tls
                else imaplib.IMAP4(self.host, self.port)
            )
        conn.login(self.user, self.password)
        return conn

    def fetch(self, state: dict[str, Any], options: FetchOptions) -> ImapFetchSession:
        return ImapFetchSession(self, state, options)

    def seed(self, state: dict[str, Any]) -> dict[str, Any]:
        """Record each folder's current highest UID so the live poll starts from the next arrival.

        History is the backfill's job, which walks a separate `backfill:<folder>` cursor.
        """
        new_state = dict(state)
        conn = self.open()
        try:
            for folder in self.folders:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    continue
                uids = search_uids(conn, last_uid=0, since=None)
                new_state[live_key(folder)] = {
                    "uidvalidity": uidvalidity(conn),
                    "last_uid": max(uids) if uids else 0,
                }
        finally:
            _logout(conn)
        return new_state


def live_key(folder: str) -> str:
    return folder


def backfill_key(folder: str) -> str:
    return f"backfill:{folder}"


def uidvalidity(conn: ImapConnection) -> str:
    """imaplib returns ('UIDVALIDITY', [b'123']); normalise to a string."""
    try:
        value = conn.response("UIDVALIDITY")[1][0]
    except (IndexError, TypeError):
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def search_uids(conn: ImapConnection, *, last_uid: int, since: datetime | None) -> list[int]:
    criteria = f"UID {last_uid + 1}:*"
    if since is not None:
        criteria = f"{criteria} SINCE {since.strftime('%d-%b-%Y')}"
    status, res = conn.uid("search", criteria)
    if status != "OK":
        raise ImapSearchError(criteria)
    raw = res[0] if res else b""
    tokens = (raw.split() if isinstance(raw, bytes) else str(raw).split()) or []
    return sorted({u for u in (int(t) for t in tokens) if u > last_uid})


class ImapSearchError(Exception):
    pass


def _logout(conn: ImapConnection) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001 - logout failures are not ingestion failures
        pass


class ImapFetchSession(CheckpointSession):
    """One poll or backfill batch. Each UID is its own checkpoint, so the cursor stops below the
    first message that was not stored."""

    def __init__(self, source: ImapSource, state: dict[str, Any], options: FetchOptions) -> None:
        super().__init__(state, options)
        self.source = source
        self._is_backfill = options.since is not None
        self._uidvalidity: dict[str, str] = {}
        self._exhausted: set[str] = set()

    def _key(self, folder: str) -> str:
        return backfill_key(folder) if self._is_backfill else live_key(folder)

    def _messages(self) -> Iterator[FetchedMessage]:
        conn = self.source.open()
        try:
            for folder in self.source.folders:
                yield from self._folder(conn, folder)
        finally:
            _logout(conn)

    def _folder(self, conn: ImapConnection, folder: str) -> Iterator[FetchedMessage]:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            self.fail(None, f"{folder}: select failed")
            return
        validity = uidvalidity(conn)
        self._uidvalidity[folder] = validity
        prev = self.state.get(self._key(folder)) or {}
        if prev.get("done"):
            return
        # SPEC-01: a UIDVALIDITY change invalidates stored UIDs, so rescan from 1 and let
        # Message-ID dedupe keep the rescan from creating duplicates.
        last_uid = int(prev.get("last_uid", 0)) if prev.get("uidvalidity") == validity else 0
        try:
            uids = search_uids(conn, last_uid=last_uid, since=self.options.since)
        except ImapSearchError:
            self.fail(None, f"{folder}: search failed")
            return
        if len(uids) <= self.options.limit:
            self._exhausted.add(folder)
        else:
            self.more_available = True
        for uid in uids[: self.options.limit]:
            provider_id = f"{folder}:{validity}:{uid}"
            self.note(folder, provider_id)
            self.checkpoint(folder, uid)
            if self.pass_over(provider_id):
                continue
            item = self._fetch_one(conn, folder, uid, provider_id)
            if item is not None:
                yield item

    def _fetch_one(
        self, conn: ImapConnection, folder: str, uid: int, provider_id: str
    ) -> FetchedMessage | None:
        try:
            status, msg = conn.uid("fetch", str(uid), "(BODY.PEEK[] INTERNALDATE)")
        except Exception as e:  # noqa: BLE001
            self.fail(provider_id, str(e))
            return None
        if status != "OK" or not msg or not isinstance(msg[0], tuple):
            self.fail(provider_id, "fetch failed")
            return None
        header, body = msg[0][0], msg[0][1]
        if not isinstance(body, bytes):
            self.fail(provider_id, "fetch returned no body")
            return None
        try:
            parsed = parse_eml(body)
        except Exception as e:  # noqa: BLE001
            self.fail(provider_id, str(e))
            return None
        return FetchedMessage(
            provider_message_id=provider_id,
            parsed=parsed,
            received_at=_internaldate(header),
        )

    def _folder_complete(self, folder: str) -> bool:
        """Only stop backfilling a folder once it is exhausted and nothing in it is outstanding."""
        if folder not in self._exhausted or self.more_available:
            return False
        return not any(pid.startswith(f"{folder}:") for pid in self.failures)

    def new_state(self) -> dict[str, Any]:
        state = dict(self.state)
        safe = self.safe_cursors()
        for folder in self.source.folders:
            validity = self._uidvalidity.get(folder)
            if validity is None:
                continue
            prev = state.get(self._key(folder)) or {}
            carried = int(prev.get("last_uid", 0)) if prev.get("uidvalidity") == validity else 0
            last_uid = int(safe.get(folder, carried))
            entry: dict[str, Any] = {"uidvalidity": validity, "last_uid": last_uid}
            if self._is_backfill and self._folder_complete(folder):
                entry["done"] = True
            state[self._key(folder)] = entry
        return state


def _internaldate(header: Any) -> datetime | None:
    """IMAP INTERNALDATE: the server's own receipt time, which the sender cannot forge."""
    if not isinstance(header, bytes):
        return None
    m = _INTERNALDATE.search(header)
    if not m:
        return None
    try:
        dt = parsedate_to_datetime(m.group(1).decode())
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
