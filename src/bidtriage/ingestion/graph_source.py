"""Microsoft Graph delta-poll source (ADR-003, SPEC-01 F1/F7).

Application permission `Mail.Read`, narrowed to the target mailbox by an application access policy
(see docs/runbooks/connect-m365-mailbox.md). Every call is a GET: nothing here marks mail read,
flags it or moves it, which is the SPEC-01 "never modify source mail" guarantee.

Network-facing; the HTTP layer is injected so tests can drive it without a mailbox.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from bidtriage.ingestion.eml import ParsedMessage, parse_eml
from bidtriage.ingestion.protocol import PollResult

GRAPH = "https://graph.microsoft.com/v1.0"
PAGE_SIZE = 50


class DeltaExpiredError(Exception):
    """Graph answered 410 Gone: the delta token is too old and the folder needs a full resync."""


class GraphSource:
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
        self._transport = transport
        self._mime_fetch = mime_fetch

    # ------------------------------------------------------------------ transport

    def _auth(self) -> str:
        if self._token:
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
        self._token = str(r.json()["access_token"])
        return self._token

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(url, params)
        r = httpx.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {self._auth()}",
                "Prefer": f"odata.maxpagesize={PAGE_SIZE}",
            },
            timeout=60,
        )
        if r.status_code == 410:
            raise DeltaExpiredError()
        r.raise_for_status()
        return dict(r.json())

    def _mime(self, message_id: str) -> bytes:
        if self._mime_fetch is not None:
            return self._mime_fetch(message_id)
        r = httpx.get(
            f"{GRAPH}/users/{self.mailbox}/messages/{message_id}/$value",
            headers={"Authorization": f"Bearer {self._auth()}"},
            timeout=120,
        )
        r.raise_for_status()
        return r.content

    # ------------------------------------------------------------------ polling

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        """Delta-query every configured folder, resuming from the stored deltaLink."""
        out: list[tuple[str, ParsedMessage]] = []
        errors: list[str] = []
        new_state = dict(state)
        more = False
        for folder in self.folders:
            url = state.get(f"delta:{folder}") or self._delta_url(folder)
            try:
                more |= self._drain_delta(folder, url, out, errors, new_state, limit)
            except DeltaExpiredError:
                # SPEC-01: 410 Gone means resync the folder from scratch. Ingestion dedupes, so a
                # full resync links duplicates rather than creating them.
                new_state.pop(f"delta:{folder}", None)
                errors.append(f"{folder}: delta token expired; full resync scheduled")
                try:
                    more |= self._drain_delta(
                        folder, self._delta_url(folder), out, errors, new_state, limit
                    )
                except DeltaExpiredError:
                    errors.append(f"{folder}: resync also returned 410")
        return PollResult(out, new_state, errors, more_available=more)

    def seed(self, state: dict[str, Any]) -> dict[str, Any]:
        """Take a delta token for the folder's current state without returning any messages.

        `$deltatoken=latest` is Graph's way of saying "start from now"; history then comes from
        `backfill` in the configured window rather than from a full initial delta sync.
        """
        new_state = dict(state)
        for folder in self.folders:
            page = self._get(f"{self._delta_url(folder)}?$deltatoken=latest", None)
            link = page.get("@odata.deltaLink") or page.get("@odata.nextLink")
            if link:
                new_state[f"delta:{folder}"] = link
        return new_state

    def backfill(self, state: dict[str, Any], *, since: datetime, limit: int = 50) -> PollResult:
        """Walk history oldest-first from `since`, a page at a time (SPEC-01 F7).

        Separate from `poll` so live mail is never starved: the caller runs one backfill batch
        between live polls and stores the cursor in `backfill:<folder>`.
        """
        out: list[tuple[str, ParsedMessage]] = []
        errors: list[str] = []
        new_state = dict(state)
        more = False
        for folder in self.folders:
            key = f"backfill:{folder}"
            if new_state.get(key) == "done":
                continue
            url = new_state.get(key) or (
                f"{GRAPH}/users/{self.mailbox}/mailFolders/{folder}/messages"
            )
            params: dict[str, Any] | None = None
            if not new_state.get(key):
                params = {
                    "$filter": f"receivedDateTime ge {_graph_time(since)}",
                    "$orderby": "receivedDateTime asc",
                    "$select": "id,internetMessageId,receivedDateTime",
                    "$top": limit,
                }
            try:
                page = self._get(url, params)
            except DeltaExpiredError:
                new_state[key] = "done"
                continue
            fetched = self._fetch_page(page, out, errors)
            next_link = page.get("@odata.nextLink")
            new_state[key] = next_link or "done"
            more |= bool(next_link)
            if fetched >= limit:
                break
        return PollResult(out, new_state, errors, more_available=more)

    # ------------------------------------------------------------------ internals

    def _delta_url(self, folder: str) -> str:
        return f"{GRAPH}/users/{self.mailbox}/mailFolders/{folder}/messages/delta"

    def _drain_delta(
        self,
        folder: str,
        url: str,
        out: list[tuple[str, ParsedMessage]],
        errors: list[str],
        new_state: dict[str, Any],
        limit: int,
    ) -> bool:
        """Follow nextLink pages until the deltaLink or `limit`. Returns True if more remain."""
        next_url: str | None = url
        while next_url and len(out) < limit:
            page = self._get(next_url, self._delta_params(next_url))
            self._fetch_page(page, out, errors)
            if "@odata.deltaLink" in page:
                new_state[f"delta:{folder}"] = page["@odata.deltaLink"]
                return False
            next_url = page.get("@odata.nextLink")
            if next_url:
                new_state[f"delta:{folder}"] = next_url
        return bool(next_url)

    @staticmethod
    def _delta_params(url: str) -> dict[str, Any] | None:
        if "deltatoken" in url.lower() or "skiptoken" in url.lower():
            return None
        return {"$select": "id,internetMessageId"}

    def _fetch_page(
        self,
        page: dict[str, Any],
        out: list[tuple[str, ParsedMessage]],
        errors: list[str],
    ) -> int:
        fetched = 0
        for item in page.get("value", []):
            if "@removed" in item:
                continue
            try:
                out.append((item["id"], parse_eml(self._mime(item["id"]))))
                fetched += 1
            except Exception as e:  # noqa: BLE001 - one unreadable message must not stop the poll
                errors.append(f"{item.get('id')}: {e}")
        return fetched


def _graph_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_basic(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()
