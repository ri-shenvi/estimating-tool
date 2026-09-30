"""IMAP UID-poll source (ADR-003 fallback). Read-only: uses EXAMINE via readonly=True so flags never change."""

from __future__ import annotations

import imaplib
from typing import Any

from bidtriage.ingestion.eml import parse_eml
from bidtriage.ingestion.protocol import PollResult


class ImapSource:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        folders: list[str] | None = None,
        tls: bool = True,
    ) -> None:
        self.host, self.port, self.user, self.password, self.tls = host, port, user, password, tls
        self.folders = folders or ["INBOX"]

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        out: list[tuple[str, Any]] = []
        errors: list[str] = []
        new_state = dict(state)
        conn = (
            imaplib.IMAP4_SSL(self.host, self.port)
            if self.tls
            else imaplib.IMAP4(self.host, self.port)
        )
        try:
            conn.login(self.user, self.password)
            for folder in self.folders:
                status, data = conn.select(folder, readonly=True)
                if status != "OK":
                    errors.append(f"{folder}: select failed")
                    continue
                uidvalidity = conn.response("UIDVALIDITY")[1][0]
                uidvalidity = (
                    uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
                )
                key = f"{folder}"
                prev = state.get(key, {})
                last_uid = prev.get("last_uid", 0) if prev.get("uidvalidity") == uidvalidity else 0
                status, res = conn.uid("search", f"UID {last_uid + 1}:*")
                uids = [
                    int(u) for u in (res[0].split() if res and res[0] else []) if int(u) > last_uid
                ]
                for uid in uids[:limit]:
                    status, msg = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                    if status != "OK" or not msg or not isinstance(msg[0], tuple):
                        errors.append(f"{folder}:{uid}: fetch failed")
                        continue
                    try:
                        out.append((f"{folder}:{uidvalidity}:{uid}", parse_eml(msg[0][1])))
                        last_uid = uid
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{folder}:{uid}: {e}")
                new_state[key] = {"uidvalidity": uidvalidity, "last_uid": last_uid}
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return PollResult(out, new_state, errors)
