"""RFC 822 parsing, forward unwrapping, quoted-history trimming (SPEC-01 F2, F4)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from typing import cast

MAX_FORWARD_DEPTH = 4

_FWD_SUBJECT = re.compile(r"^\s*(?:(?:fwd?|fw)\s*:\s*)+", re.I)
_FWD_ONE = re.compile(r"^\s*(?:fwd?|fw)\s*:\s*", re.I)
_HDR_KEYS = "From|Sent|Date|To|Cc|Bcc|Subject|Reply-To|Importance|Attachments"
_HDR_BLOCK = rf"(?:[ \t>]*\*?(?:{_HDR_KEYS})\*?[ \t]*:[^\n]*\n)+"
# Outlook / Gmail put a separator line above the quoted original...
_INLINE_FWD_SEP = re.compile(
    r"(?:^|\n)[ \t>]*(?:-{2,}[ \t]*(?:Original Message|Forwarded message)[ \t]*-{2,}|_{5,})[ \t]*\n+"
    rf"(?P<hdr>{_HDR_BLOCK})",
    re.I,
)
# ...but some clients inline the original with nothing but a bare header block.
_INLINE_FWD_BARE = re.compile(
    rf"(?:^|\n)(?P<hdr>[ \t>]*\*?From\*?[ \t]*:[^\n]*\n(?:[ \t>]*\*?(?:{_HDR_KEYS})\*?[ \t]*:[^\n]*\n)+)",
    re.I,
)
_HDR_LINE = re.compile(rf"^[ \t>]*\*?({_HDR_KEYS})\*?[ \t]*:[ \t]*(.*)$", re.I)
_QUOTE_MARKERS = [
    re.compile(r"\n\s*-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"\nOn .{5,120} wrote:\s*\n", re.I),
    re.compile(r"\nFrom:\s.*\nSent:\s.*\n", re.I),
    re.compile(r"\n_{5,}\n"),
]


@dataclass
class ParsedAttachment:
    filename: str
    mime: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class ParsedMessage:
    internet_message_id: str | None
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    subject: str
    sent_at: datetime | None
    sent_at_confidence: str
    body_text: str
    body_html: str
    body_trimmed: str
    headers: dict[str, str]
    in_reply_to: str | None
    references: list[str]
    attachments: list[ParsedAttachment] = field(default_factory=list)
    forwarded_by: str | None = None
    forward_note: str | None = None
    forward_chain: list[str] = field(default_factory=list)
    received_at: datetime | None = None

    @property
    def content_hash(self) -> str:
        subj = _FWD_SUBJECT.sub("", self.subject).strip().lower()
        body = " ".join(self.body_trimmed.split()).lower()
        att = ",".join(sorted(a.sha256 for a in self.attachments))
        return hashlib.sha256(f"{subj}\n{body}\n{att}".encode()).hexdigest()


def html_to_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    s = re.sub(
        r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", lambda m: f"{m.group(2)} ({m.group(1)})", s
    )
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def trim_quoted(text: str) -> str:
    cut = len(text)
    for pat in _QUOTE_MARKERS:
        m = pat.search(text)
        if m and m.start() < cut:
            cut = m.start()
    return text[:cut].strip()


def _decode_header(value: object) -> str:
    """Decode an RFC 2047 header. Mislabelled charsets degrade to replacement characters."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(value)


def _decode_date(msg: EmailMessage) -> tuple[datetime | None, str]:
    raw = msg.get("Date")
    if not raw:
        return None, "low"
    try:
        dt = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None, "low"
    if dt is None:
        return None, "low"
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC), "low"
    return dt, "high"


def received_at_from_headers(msg: EmailMessage) -> datetime | None:
    """Server receipt time from the topmost `Received:` trace header (SPEC-01 F2).

    Used when the sender's `Date:` header is missing or unparseable.
    """
    for raw in msg.get_all("Received", []) or []:
        _, _, tail = str(raw).rpartition(";")
        if not tail.strip():
            continue
        try:
            dt = parsedate_to_datetime(tail.strip())
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    raw = [_decode_header(v) for v in (msg.get_all(header, []) or [])]
    return [addr.lower() for _, addr in getaddresses(raw) if addr]


def _part_text(part: EmailMessage) -> str:
    """Decode a text part, tolerating a missing or lying charset label.

    Line endings are normalised to \n so the forward and quote patterns below never have to allow
    for a stray \r.
    """
    try:
        content = part.get_content()
        return _lf(content if isinstance(content, str) else str(content))
    except (LookupError, UnicodeDecodeError, ValueError):
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return _lf(payload.decode(charset, errors="replace"))
        except LookupError:
            return _lf(payload.decode("utf-8", errors="replace"))


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_inline_image(part: EmailMessage) -> bool:
    """Signature logos and other body images are ignored (SPEC-01 F5)."""
    if not part.get_content_maintype() == "image":
        return False
    disp = (part.get_content_disposition() or "").lower()
    return disp != "attachment" or bool(part.get("Content-ID"))


def _body_parts(msg: EmailMessage) -> tuple[str, str, list[ParsedAttachment], list[EmailMessage]]:
    text, html = "", ""
    attachments: list[ParsedAttachment] = []
    rfc822: list[EmailMessage] = []

    def visit(part: EmailMessage) -> None:
        nonlocal text, html
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) else payload
            if isinstance(inner, EmailMessage):
                rfc822.append(inner)
            return
        if part.is_multipart():
            for sub in part.iter_parts():
                visit(cast(EmailMessage, sub))
            return
        disp = (part.get_content_disposition() or "").lower()
        if disp == "attachment" or part.get_filename():
            if _is_inline_image(part):
                return
            try:
                payload_bytes = part.get_payload(decode=True)
                data = payload_bytes if isinstance(payload_bytes, bytes) else b""
            except Exception:  # noqa: BLE001 - a corrupt part must not lose the message
                data = b""
            attachments.append(
                ParsedAttachment(
                    filename=_decode_header(part.get_filename()) or "attachment",
                    mime=ctype,
                    data=data,
                )
            )
            return
        if ctype == "text/plain" and not text:
            text = _part_text(part)
        elif ctype == "text/html" and not html:
            html = _part_text(part)

    visit(msg)
    return text, html, attachments, rfc822


def _parse_inline_forward(body: str) -> tuple[dict[str, str], str, str] | None:
    """Peel one inline-forward layer. Returns (headers, note_above, original_body)."""
    m = _INLINE_FWD_SEP.search(body) or _INLINE_FWD_BARE.search(body)
    if not m:
        return None
    hdrs: dict[str, str] = {}
    for line in m.group("hdr").splitlines():
        hm = _HDR_LINE.match(line)
        if hm:
            hdrs[hm.group(1).lower()] = _decode_header(hm.group(2).strip())
    if "from" not in hdrs:
        return None
    return hdrs, body[: m.start()].strip(), body[m.end() :]


def _peel_inline_forwards(body: str) -> tuple[list[tuple[dict[str, str], str]], str]:
    """Peel every inline-forward layer, outermost first. Returns (layers, innermost body)."""
    layers: list[tuple[dict[str, str], str]] = []
    rest = body
    while len(layers) < MAX_FORWARD_DEPTH:
        peeled = _parse_inline_forward(rest)
        if peeled is None:
            break
        hdrs, note, rest = peeled
        layers.append((hdrs, note))
    return layers, rest


def _header_date(hdrs: dict[str, str]) -> datetime | None:
    """Parse the `Sent:`/`Date:` line of an inlined header block; clients write it many ways."""
    for key in ("sent", "date"):
        raw = hdrs.get(key)
        if not raw:
            continue
        dt: datetime | None
        try:
            from dateutil import parser as dparser

            dt = dparser.parse(raw)
        except (ValueError, OverflowError, TypeError):
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                dt = None
        if dt is None:
            continue
        if dt.tzinfo is None:
            from bidtriage.core.clock import BUSINESS_TZ

            dt = dt.replace(tzinfo=BUSINESS_TZ)
        return dt
    return None


def parse_eml(raw: bytes) -> ParsedMessage:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return _parse_message(msg)


def parse_rfc822(msg: EmailMessage) -> ParsedMessage:
    """Parse an already-constructed EmailMessage (used by the `.msg` reader)."""
    return _parse_message(msg)


def _parse_message(msg: EmailMessage, depth: int = 0) -> ParsedMessage:
    text, html, attachments, rfc822 = _body_parts(msg)
    if not text and html:
        text = html_to_text(html)
    sent_at, conf = _decode_date(msg)
    received_at = received_at_from_headers(msg)
    from_pairs = getaddresses([_decode_header(v) for v in (msg.get_all("From", []) or [])])
    from_name, from_addr = from_pairs[0] if from_pairs else ("", "")
    subject = _decode_header(msg.get("Subject", ""))
    headers = {k: _decode_header(v) for k, v in msg.items()}
    refs = [r for r in str(msg.get("References", "") or "").split() if r]

    parsed = ParsedMessage(
        internet_message_id=(str(msg.get("Message-ID")).strip() if msg.get("Message-ID") else None),
        from_addr=from_addr.lower(),
        from_name=from_name,
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        subject=subject,
        sent_at=sent_at,
        sent_at_confidence=conf,
        body_text=text,
        body_html=html,
        body_trimmed=trim_quoted(text),
        headers=headers,
        in_reply_to=(str(msg.get("In-Reply-To")).strip() if msg.get("In-Reply-To") else None),
        references=refs,
        attachments=attachments,
        received_at=received_at,
    )

    if not (_FWD_ONE.match(subject) or rfc822) or depth >= MAX_FORWARD_DEPTH:
        return parsed
    if rfc822:
        return _unwrap_rfc822(parsed, rfc822[0], attachments, depth)
    return _unwrap_inline(parsed) or parsed


def _unwrap_rfc822(
    outer: ParsedMessage,
    attached: EmailMessage,
    attachments: list[ParsedAttachment],
    depth: int,
) -> ParsedMessage:
    """The original arrived intact as a message/rfc822 part: use it, keeping the forward trail."""
    inner = _parse_message(attached, depth + 1)
    inner.forward_chain = [outer.from_addr, *inner.forward_chain]
    inner.forwarded_by = outer.from_addr
    inner.forward_note = outer.body_trimmed or inner.forward_note
    inner.attachments = inner.attachments or attachments
    inner.received_at = outer.received_at or inner.received_at
    return inner


def _unwrap_inline(outer: ParsedMessage) -> ParsedMessage | None:
    """The original was inlined as text: rebuild it from the quoted header block(s)."""
    layers, body = _peel_inline_forwards(outer.body_text)
    if not layers:
        return None
    original_hdrs, _ = layers[-1]
    forwarders = [outer.from_addr] + [
        addr for hdrs, _ in layers[:-1] for _, addr in getaddresses([hdrs.get("from", "")]) if addr
    ]
    orig_pairs = getaddresses([original_hdrs.get("from", "")])
    o_name, o_addr = orig_pairs[0] if orig_pairs else ("", "")
    o_date = _header_date(original_hdrs)
    subject = _FWD_SUBJECT.sub("", original_hdrs.get("subject") or outer.subject).strip()
    return ParsedMessage(
        internet_message_id=None,
        from_addr=o_addr.lower(),
        from_name=o_name,
        to=_addresses_from_header(original_hdrs.get("to")) or outer.to,
        cc=_addresses_from_header(original_hdrs.get("cc")),
        subject=subject,
        sent_at=o_date or outer.sent_at,
        sent_at_confidence="low",
        body_text=body,
        body_html="",
        body_trimmed=trim_quoted(body),
        headers=outer.headers,
        in_reply_to=None,
        references=outer.references,
        attachments=outer.attachments,
        forwarded_by=outer.from_addr,
        forward_note=(layers[0][1] or None),
        forward_chain=[f.lower() for f in forwarders if f],
        received_at=outer.received_at,
    )


def _addresses_from_header(value: str | None) -> list[str]:
    if not value:
        return []
    return [addr.lower() for _, addr in getaddresses([value]) if addr]
