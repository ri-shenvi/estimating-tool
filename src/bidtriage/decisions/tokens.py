"""Signed one-click action links (SPEC-06 F2). v1.<payload>.<sig>; single-use enforced by caller."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bidtriage.core.crypto import sign, verify

VERSION = "v1"


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class ActionToken:
    opportunity_id: str
    action: str
    recipient_id: str
    issued_at: datetime
    nonce: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_action(
    opportunity_id: str,
    action: str,
    recipient_id: str,
    secret_key: str,
    *,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> str:
    issued_at = issued_at or datetime.now(tz=UTC)
    payload = json.dumps(
        {
            "o": opportunity_id,
            "a": action,
            "r": recipient_id,
            "t": int(issued_at.timestamp()),
            "n": nonce or secrets.token_urlsafe(8),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{VERSION}.{_b64(payload)}.{sign(payload, secret_key)}"


def verify_action(
    token: str, secret_key: str, *, ttl: timedelta = timedelta(days=7), now: datetime | None = None
) -> ActionToken:
    now = now or datetime.now(tz=UTC)
    try:
        version, p64, sig = token.split(".")
    except ValueError as e:
        raise TokenError("malformed") from e
    if version != VERSION:
        raise TokenError("unsupported version")
    payload = _unb64(p64)
    if not verify(payload, sig, secret_key):
        raise TokenError("bad signature")
    data = json.loads(payload)
    issued = datetime.fromtimestamp(data["t"], tz=UTC)
    if now - issued > ttl:
        raise TokenError("expired")
    if issued - now > timedelta(minutes=5):
        raise TokenError("issued in the future")
    return ActionToken(data["o"], data["a"], data["r"], issued, data["n"])
