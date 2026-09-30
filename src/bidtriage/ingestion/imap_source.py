"""IMAP UID-poll source (ADR-003 fallback, SPEC-01 F1/F7).

Read-only by construction: the folder is opened with `readonly=True` (EXAMINE, not SELECT) and
bodies are fetched with `BODY.PEEK[]`, so neither \\Seen nor any other flag changes. That is the
SPEC-01 "never modify source mail" guarantee for this transport.
"""

from __future__ import annotations

import imaplib
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from bidtriage.ingestion.eml import ParsedMessage, parse_eml
from bidtriage.ingestion.protocol import PollResult


class ImapConnection(Protocol):
    def login(self, user: str, password: str) -> Any: ...
    def select(self, mailbox: str, readonly: bool = ...) -> Any: ...
    def response(self, which: str) -> Any: ...
    def uid(self, command: str, *args: str) -> Any: ...
    def logout(self) -> Any: ...


class ImapSource:
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

    def _open(self) -> ImapConnection:
        if self._connect is not None:
            return self._connect()
        conn: ImapConnection = (
            imaplib.IMAP4_SSL(self.host, self.port)
            if self.tls
            else imaplib.IMAP4(self.host, self.port)
        )
        return conn

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        return self._scan(state, limit=limit, since=None)

    def seed(self, state: dict[str, Any]) -> dict[str, Any]:
        """Record each folder's current highest UID so `poll` only returns mail that arrives next.

        History is the backfill's job, which walks a separate `backfill:<folder>` cursor.
        """
        new_state = dict(state)
        conn = self._open()
        try:
            conn.login(self.user, self.password)
            for folder in self.folders:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    continue
                uidvalidity = _first(conn.response("UIDVALIDITY"))
                uids = self._search(conn, 0, None, [], folder)
                new_state[folder] = {
                    "uidvalidity": uidvalidity,
                    "last_uid": max(uids) if uids else 0,
                }
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return new_state

    def backfill(self, state: dict[str, Any], *, since: datetime, limit: int = 50) -> PollResult:
        """Walk the folder oldest-first from `since` using a separate `backfill:<folder>` cursor."""
        return self._scan(state, limit=limit, since=since)

    def _scan(self, state: dict[str, Any], *, limit: int, since: datetime | None) -> PollResult:
        out: list[tuple[str, ParsedMessage]] = []
        errors: list[str] = []
        new_state = dict(state)
        more = False
        conn = self._open()
        try:
            conn.login(self.user, self.password)
            for folder in self.folders:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    errors.append(f"{folder}: select failed")
                    continue
                uidvalidity = _first(conn.response("UIDVALIDITY"))
                key = f"backfill:{folder}" if since is not None else folder
                prev = state.get(key) or {}
                if prev.get("done"):
                    continue
                # SPEC-01: a UIDVALIDITY change invalidates stored UIDs, so rescan from 1 and let
                # Message-ID dedupe keep the rescan from creating duplicates.
                last_uid = (
                    int(prev.get("last_uid", 0)) if prev.get("uidvalidity") == uidvalidity else 0
                )
                uids = self._search(conn, last_uid, since, errors, folder)
                for uid in uids[:limit]:
                    raw = self._fetch(conn, uid, errors, folder)
                    if raw is None:
                        continue
                    try:
                        out.append((f"{folder}:{uidvalidity}:{uid}", parse_eml(raw)))
                    except Exception as e:  # noqa: BLE001 - skip, do not abort the folder
                        errors.append(f"{folder}:{uid}: {e}")
                        continue
                    last_uid = max(last_uid, uid)
                remaining = len(uids) > limit
                more |= remaining
                new_state[key] = {
                    "uidvalidity": uidvalidity,
                    "last_uid": last_uid,
                    **({"done": True} if since is not None and not remaining else {}),
                }
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - logout failures are not ingestion failures
                pass
        return PollResult(out, new_state, errors, more_available=more)

    @staticmethod
    def _search(
        conn: ImapConnection,
        last_uid: int,
        since: datetime | None,
        errors: list[str],
        folder: str,
    ) -> list[int]:
        criteria = f"UID {last_uid + 1}:*"
        if since is not None:
            criteria = f"{criteria} SINCE {since.strftime('%d-%b-%Y')}"
        status, res = conn.uid("search", criteria)
        if status != "OK":
            errors.append(f"{folder}: search failed")
            return []
        raw = res[0] if res else b""
        tokens = (raw.split() if isinstance(raw, bytes) else str(raw).split()) or []
        return sorted({u for u in (int(t) for t in tokens) if u > last_uid})

    @staticmethod
    def _fetch(conn: ImapConnection, uid: int, errors: list[str], folder: str) -> bytes | None:
        status, msg = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
        if status != "OK" or not msg or not isinstance(msg[0], tuple):
            errors.append(f"{folder}:{uid}: fetch failed")
            return None
        body = msg[0][1]
        return body if isinstance(body, bytes) else None


def _first(response: Any) -> str:
    """imaplib returns ('UIDVALIDITY', [b'123']); normalise to a string."""
    try:
        value = response[1][0]
    except (IndexError, TypeError):
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)
