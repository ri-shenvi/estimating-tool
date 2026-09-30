"""Microsoft Graph delta-poll source (ADR-003, SPEC-01 F1/F7, SPEC-10 F1/F2/F3/F7).

Application permission `Mail.Read`, narrowed to the target mailbox by an application access policy
(see docs/runbooks/connect-m365-mailbox.md). Every call is a GET: nothing here marks mail read,
flags it or moves it, which is the SPEC-01 "never modify source mail" guarantee.

Messages are fetched lazily, one MIME body at a time, and the delta link is only kept when every
message it covers was durably stored. Network-facing; the HTTP layer is injected so tests can drive
it without a mailbox.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from bidtriage.ingestion.eml import parse_eml
from bidtriage.ingestion.protocol import (
    CheckpointSession,
    FetchedMessage,
    FetchOptions,
)

GRAPH = "https://graph.microsoft.com/v1.0"
_CLEAR = object()
"""Checkpoint value meaning "remove this folder's stored cursor"."""
PAGE_SIZE = 50
TOKEN_REFRESH_MARGIN = timedelta(seconds=120)
SELECT_FIELDS = "id,internetMessageId,receivedDateTime"


class DeltaExpiredError(Exception):
    """Graph answered 410 Gone: the delta token is too old and the folder needs a full resync."""


class GraphAuthError(Exception):
    """Graph rejected the bearer token even after a refresh."""


class GraphSource:
    can_backfill = True

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        folders: list[str] | None = None,
        *,
        transport: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None,
        mime_fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox
        self.folders = folders or ["inbox"]
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._transport = transport
        self._mime_fetch = mime_fetch

    # ------------------------------------------------------------------ auth

    def _auth(self, *, force_refresh: bool = False) -> str:
        """Bearer token, refreshed before it expires (SPEC-10 F3).

        The worker keeps a GraphSource for its whole life, so a token cached without an expiry
        would 401 every call about an hour after start-up and report a healthy mailbox as down.
        """
        now = datetime.now(tz=UTC)
        fresh = (
            self._token is not None
            and self._token_expires_at is not None
            and now + TOKEN_REFRESH_MARGIN < self._token_expires_at
        )
        if fresh and not force_refresh:
            assert self._token is not None
            return self._token
        r = httpx.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        self._token = str(payload["access_token"])
        self._token_expires_at = now + timedelta(seconds=int(payload.get("expires_in", 3600)))
        return self._token

    # ------------------------------------------------------------------ transport

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(url, params)
        for attempt in (1, 2):
            r = httpx.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._auth(force_refresh=attempt == 2)}",
                    "Prefer": f"odata.maxpagesize={PAGE_SIZE}",
                },
                timeout=60,
            )
            if r.status_code == 410:
                raise DeltaExpiredError()
            if r.status_code == 401 and attempt == 1:
                continue  # token revoked or rotated mid-run: refresh once, then give up
            if r.status_code == 401:
                raise GraphAuthError("Graph returned 401 after a token refresh")
            r.raise_for_status()
            return dict(r.json())
        raise GraphAuthError("unreachable")

    def _mime(self, message_id: str) -> bytes:
        if self._mime_fetch is not None:
            return self._mime_fetch(message_id)
        for attempt in (1, 2):
            r = httpx.get(
                f"{GRAPH}/users/{self.mailbox}/messages/{message_id}/$value",
                headers={"Authorization": f"Bearer {self._auth(force_refresh=attempt == 2)}"},
                timeout=120,
            )
            if r.status_code == 401 and attempt == 1:
                continue
            r.raise_for_status()
            return r.content
        raise GraphAuthError("unreachable")

    # ------------------------------------------------------------------ fetching

    def fetch(self, state: dict[str, Any], options: FetchOptions) -> GraphFetchSession:
        return GraphFetchSession(self, state, options)

    def seed(self, state: dict[str, Any]) -> dict[str, Any]:
        """Take a delta token for the folder's current state without returning any messages.

        `$deltatoken=latest` is Graph's way of saying "start from now"; history then comes from a
        backfill within the configured window rather than from a full initial delta sync.
        """
        new_state = dict(state)
        for folder in self.folders:
            page = self._get(f"{self.delta_url(folder)}?$deltatoken=latest", None)
            link = page.get("@odata.deltaLink") or page.get("@odata.nextLink")
            if link:
                new_state[live_key(folder)] = link
        return new_state

    def delta_url(self, folder: str) -> str:
        return f"{GRAPH}/users/{self.mailbox}/mailFolders/{folder}/messages/delta"

    def list_url(self, folder: str) -> str:
        return f"{GRAPH}/users/{self.mailbox}/mailFolders/{folder}/messages"


def live_key(folder: str) -> str:
    return f"delta:{folder}"


def backfill_key(folder: str) -> str:
    return f"backfill:{folder}"


class GraphFetchSession(CheckpointSession):
    """One poll or backfill batch over the configured folders.

    Each page link is a checkpoint. `new_state` keeps only the newest checkpoint whose pages were
    entirely settled, so a message whose MIME fetch failed stays in front of the cursor and is
    retried on the next poll (SPEC-10 F1).
    """

    can_stop_midstream = False
    """A delta page is only resumable once it is finished, so the batch ends at a page boundary."""

    def __init__(self, source: GraphSource, state: dict[str, Any], options: FetchOptions) -> None:
        super().__init__(state, options)
        self.source = source
        self._resynced: set[str] = set()
        self._is_backfill = options.since is not None

    def _key(self, folder: str) -> str:
        return backfill_key(folder) if self._is_backfill else live_key(folder)

    def _messages(self) -> Iterator[FetchedMessage]:
        for folder in self.source.folders:
            if self._is_backfill and self.state.get(self._key(folder)) == "done":
                continue
            yield from self._folder(folder)

    def _folder(self, folder: str) -> Iterator[FetchedMessage]:
        start_url, params = self._start(folder)
        url: str | None = start_url
        while url:
            try:
                page = self.source._get(url, params)
            except DeltaExpiredError:
                # SPEC-01: 410 Gone means resync this folder from scratch. Ingestion dedupes, so a
                # resync links duplicates rather than creating them.
                if folder in self._resynced:
                    self.fail(None, f"{folder}: resync also returned 410")
                    return
                self._resynced.add(folder)
                self.errors.append(f"{folder}: delta token expired; full resync scheduled")
                url, params = self._start(folder, resync=True)
                continue
            params = None
            yield from self._page(folder, page)
            url = str(page["@odata.nextLink"]) if page.get("@odata.nextLink") else None
            if url:
                self.checkpoint(folder, url)
            elif "@odata.deltaLink" in page:
                self.checkpoint(folder, page["@odata.deltaLink"])
            else:
                self.checkpoint(folder, "done" if self._is_backfill else _CLEAR)
            if self.budget_spent:
                # Stop here rather than mid-page: the cursor we just checkpointed is the only
                # resumable position, so the next poll continues from it (SPEC-10 F1/F2).
                self.more_available = self.more_available or url is not None
                return

    def _start(self, folder: str, *, resync: bool = False) -> tuple[str, dict[str, Any] | None]:
        stored = None if resync else self.state.get(self._key(folder))
        if self._is_backfill:
            if stored and stored != "done":
                return str(stored), None
            assert self.options.since is not None
            return self.source.list_url(folder), {
                "$filter": f"receivedDateTime ge {_graph_time(self.options.since)}",
                "$orderby": "receivedDateTime asc",
                "$select": SELECT_FIELDS,
                "$top": min(self.options.limit, PAGE_SIZE),
            }
        if stored:
            return str(stored), None
        return self.source.delta_url(folder), {"$select": SELECT_FIELDS}

    def _page(self, folder: str, page: dict[str, Any]) -> Iterator[FetchedMessage]:
        for item in page.get("value", []):
            if "@removed" in item:
                continue
            provider_id = str(item.get("id", ""))
            if not provider_id:
                continue
            self.note(folder, provider_id)
            if self.pass_over(provider_id):
                continue
            try:
                raw = self.source._mime(provider_id)
                parsed = parse_eml(raw)
            except Exception as e:  # noqa: BLE001 - recorded, and the cursor stays behind it
                self.fail(provider_id, str(e))
                continue
            yield FetchedMessage(
                provider_message_id=provider_id,
                parsed=parsed,
                received_at=_graph_parse_time(item.get("receivedDateTime")),
            )

    def new_state(self) -> dict[str, Any]:
        state = dict(self.state)
        safe = self.safe_cursors()
        for folder, cursor in safe.items():
            if cursor is _CLEAR:
                state.pop(self._key(folder), None)
            else:
                state[self._key(folder)] = cursor
        for folder in self._resynced:
            # The stored token was rejected as expired, so it must never be written back: drop it
            # and let the next poll start another resync.
            if folder not in safe:
                state.pop(self._key(folder), None)
        return state


def _graph_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _graph_parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        try:
            return parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None


def encode_basic(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()
