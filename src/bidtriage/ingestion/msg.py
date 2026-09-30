"""Outlook `.msg` (MAPI / compound-file) reading for manual upload (SPEC-01 F1).

`.msg` has no MIME envelope, so the interesting MAPI properties are read out of the compound file
and reassembled into a ParsedMessage that looks exactly like a parsed `.eml`. Where Outlook kept
the original transport headers (property 0x007D) we reuse them, which restores Message-ID,
References and the Received trail for free.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email import message_from_string, policy
from email.message import EmailMessage
from typing import Any, Protocol

from bidtriage.ingestion.eml import (
    ParsedAttachment,
    ParsedMessage,
    html_to_text,
    parse_rfc822,
    trim_quoted,
)

# MAPI property tags, <id><type>: 001E=8-bit string, 001F=Unicode string, 0102=binary.
P_SUBJECT = "0037"
P_BODY = "1000"
P_BODY_HTML = "1013"
P_TRANSPORT_HEADERS = "007D"
P_SENDER_NAME = "0C1A"
P_SENDER_EMAIL = "0C1F"
P_SENT_REPRESENTING_EMAIL = "0065"
P_DISPLAY_TO = "0E04"
P_DISPLAY_CC = "0E03"
P_MESSAGE_ID = "1035"
P_IN_REPLY_TO = "1042"
P_REFERENCES = "1039"
P_ATTACH_FILENAME_LONG = "3707"
P_ATTACH_FILENAME = "3704"
P_ATTACH_MIME = "370E"
P_ATTACH_DATA = "3701"
P_CLIENT_SUBMIT_TIME = "0039"
P_DELIVERY_TIME = "0E06"

_SUBSTG = re.compile(r"__substg1\.0_(?P<tag>[0-9A-F]{8})", re.I)
_SMTP = re.compile(r"[\w.%+-]+@[\w.-]+\.\w+")
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


class _Ole(Protocol):
    def listdir(self, streams: bool = ..., storages: bool = ...) -> list[list[str]]: ...
    def openstream(self, filename: Any) -> Any: ...


def is_msg(data: bytes) -> bool:
    return data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def parse_msg(data: bytes) -> ParsedMessage:
    import olefile

    with olefile.OleFileIO(data) as ole:
        return parse_ole(ole)


def parse_ole(ole: _Ole) -> ParsedMessage:
    """Build a ParsedMessage from an open compound file (separated out so it can be faked in tests)."""
    root = _read_properties(ole, prefix=[])
    headers_raw = root.get(P_TRANSPORT_HEADERS, "")
    subject = _as_str(root.get(P_SUBJECT)) or ""
    body_html = _as_str(root.get(P_BODY_HTML)) or ""
    body_text = _as_str(root.get(P_BODY)) or (html_to_text(body_html) if body_html else "")
    attachments = _read_attachments(ole)

    if headers_raw:
        # The original transport headers survived: parse them and graft the MAPI body on.
        msg = message_from_string(_as_str(headers_raw) or "", policy=policy.default)
        assert isinstance(msg, EmailMessage)
        parsed = parse_rfc822(msg)
        parsed.body_text = body_text or parsed.body_text
        parsed.body_html = body_html or parsed.body_html
        parsed.body_trimmed = trim_quoted(parsed.body_text)
        parsed.subject = parsed.subject or subject
        parsed.attachments = attachments or parsed.attachments
        return parsed

    # MAPI sender properties hold anything from a bare address to "Name <addr>" to an X.500 path.
    raw_sender = (
        _as_str(root.get(P_SENDER_EMAIL)) or _as_str(root.get(P_SENT_REPRESENTING_EMAIL)) or ""
    )
    sender = next(iter(_SMTP.findall(raw_sender)), "")
    sent_at = _filetime(root.get(P_CLIENT_SUBMIT_TIME)) or _filetime(root.get(P_DELIVERY_TIME))
    message_id = _as_str(root.get(P_MESSAGE_ID)) or None
    return ParsedMessage(
        internet_message_id=message_id.strip() if message_id else None,
        from_addr=sender.lower(),
        from_name=_as_str(root.get(P_SENDER_NAME)) or "",
        to=_SMTP.findall(_as_str(root.get(P_DISPLAY_TO)) or ""),
        cc=_SMTP.findall(_as_str(root.get(P_DISPLAY_CC)) or ""),
        subject=subject,
        sent_at=sent_at,
        sent_at_confidence="high" if sent_at else "low",
        body_text=body_text,
        body_html=body_html,
        body_trimmed=trim_quoted(body_text),
        headers={"X-Bidtriage-Source-Format": "msg"},
        in_reply_to=_as_str(root.get(P_IN_REPLY_TO)) or None,
        references=(_as_str(root.get(P_REFERENCES)) or "").split(),
        attachments=attachments,
        received_at=_filetime(root.get(P_DELIVERY_TIME)),
    )


def _read_properties(ole: _Ole, *, prefix: list[str]) -> dict[str, str | bytes]:
    """Collect `__substg1.0_XXXXYYYY` streams directly under `prefix` into {property id: value}."""
    out: dict[str, str | bytes] = {}
    depth = len(prefix)
    for entry in ole.listdir(streams=True, storages=False):
        if len(entry) != depth + 1 or entry[:depth] != prefix:
            continue
        m = _SUBSTG.match(entry[-1])
        if not m:
            continue
        tag = m.group("tag").upper()
        prop, ptype = tag[:4], tag[4:]
        raw = ole.openstream(entry).read()
        out[prop] = _decode_value(raw, ptype)
    return out


def _decode_value(raw: bytes, ptype: str) -> str | bytes:
    if ptype == "001F":
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    if ptype == "001E":
        return raw.decode("cp1252", errors="replace").rstrip("\x00")
    return raw


def _as_str(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return value


def _filetime(value: str | bytes | None) -> datetime | None:
    """Windows FILETIME: 100-nanosecond ticks since 1601-01-01 UTC."""
    if not isinstance(value, bytes) or len(value) < 8:
        return None
    ticks = int.from_bytes(value[:8], "little")
    if ticks == 0:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=ticks / 10)
    except OverflowError:
        return None


def _read_attachments(ole: _Ole) -> list[ParsedAttachment]:
    out: list[ParsedAttachment] = []
    storages = sorted(
        {e[0] for e in ole.listdir(streams=True, storages=False) if e[0].startswith("__attach")}
    )
    for storage in storages:
        props = _read_properties(ole, prefix=[storage])
        data = props.get(P_ATTACH_DATA)
        if not isinstance(data, bytes):
            continue
        name = (
            _as_str(props.get(P_ATTACH_FILENAME_LONG))
            or _as_str(props.get(P_ATTACH_FILENAME))
            or "attachment"
        )
        mime = _as_str(props.get(P_ATTACH_MIME)) or "application/octet-stream"
        out.append(ParsedAttachment(filename=name, mime=mime, data=data))
    return out


def parse_upload(filename: str, data: bytes) -> ParsedMessage:
    """Parse an admin-uploaded mail file, choosing the reader by content then by extension."""
    from bidtriage.ingestion.eml import parse_eml

    if is_msg(data) or filename.lower().endswith(".msg"):
        return parse_msg(data)
    return parse_eml(data)
