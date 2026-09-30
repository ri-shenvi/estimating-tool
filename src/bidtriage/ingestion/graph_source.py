"""Microsoft Graph delta-poll source (ADR-003). Application permission Mail.Read scoped by access policy.

Network-facing; exercised manually and in staging, not in unit tests.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from bidtriage.ingestion.eml import parse_eml
from bidtriage.ingestion.protocol import PollResult

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphSource:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        folders: list[str] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox
        self.folders = folders or ["inbox"]
        self._token: str | None = None

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
        self._token = r.json()["access_token"]
        return self._token

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._auth()}", "Prefer": "odata.maxpagesize=50"},
            timeout=60,
        )
        if r.status_code == 410:
            raise DeltaExpiredError()
        r.raise_for_status()
        return r.json()

    def _mime(self, message_id: str) -> bytes:
        r = httpx.get(
            f"{GRAPH}/users/{self.mailbox}/messages/{message_id}/$value",
            headers={"Authorization": f"Bearer {self._auth()}"},
            timeout=120,
        )
        r.raise_for_status()
        return r.content

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        out: list[tuple[str, Any]] = []
        errors: list[str] = []
        new_state = dict(state)
        for folder in self.folders:
            url = (
                state.get(f"delta:{folder}")
                or f"{GRAPH}/users/{self.mailbox}/mailFolders/{folder}/messages/delta"
            )
            try:
                while url and len(out) < limit:
                    page = self._get(
                        url,
                        params={"$select": "id,internetMessageId"}
                        if "deltatoken" not in url and "skiptoken" not in url
                        else None,
                    )
                    for item in page.get("value", []):
                        if "@removed" in item:
                            continue
                        try:
                            out.append((item["id"], parse_eml(self._mime(item["id"]))))
                        except Exception as e:  # noqa: BLE001
                            errors.append(f"{item.get('id')}: {e}")
                    url = page.get("@odata.nextLink")
                    if "@odata.deltaLink" in page:
                        new_state[f"delta:{folder}"] = page["@odata.deltaLink"]
                        break
            except DeltaExpiredError:
                new_state.pop(f"delta:{folder}", None)
                errors.append(f"{folder}: delta token expired; full resync scheduled")
        return PollResult(out, new_state, errors)


class DeltaExpiredError(Exception):
    pass


def encode_basic(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()
