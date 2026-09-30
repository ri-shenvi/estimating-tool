"""RFC 822 parsing, forward unwrapping, quoted-history trimming (SPEC-01 F2, F4)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from typing import cast

_FWD_SUBJECT = re.compile(r"^\s*(fwd?|fw)\s*:\s*", re.I)
_INLINE_FWD = re.compile(
    r"(?:^|\n)\s*(?:-{2,}\s*(?:Original Message|Forwarded message)\s*-{2,}|_{5,})\s*\n(?P<hdr>(?:.*\n){1,8}?)\n",
    re.I,
)
_HDR_LINE = re.compile(r"^\s*\*?(From|Sent|Date|To|Subject|Cc)\*?\s*:\s*(.*)$", re.I)
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


def _decode_date(msg: EmailMessage) -> tuple[datetime | None, str]:
    raw = msg.get("Date")
    if not raw:
        return None, "missing"
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None, "low"
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC), "low"
    return dt, "high"


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    return [addr.lower() for _, addr in getaddresses(msg.get_all(header, [])) if addr]


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
            try:
                payload_bytes = part.get_payload(decode=True)
                data = payload_bytes if isinstance(payload_bytes, bytes) else b""
            except Exception:
                data = b""
            attachments.append(
                ParsedAttachment(
                    filename=part.get_filename() or "attachment", mime=ctype, data=data
                )
            )
            return
        if ctype == "text/plain" and not text:
            text = part.get_content()
        elif ctype == "text/html" and not html:
            html = part.get_content()

    visit(msg)
    return text, html, attachments, rfc822


def _parse_inline_forward(body: str) -> tuple[dict[str, str], str, str] | None:
    """Detect an Outlook-style inline forward. Returns (headers, note_above, original_body)."""
    m = _INLINE_FWD.search(body)
    if not m:
        return None
    hdrs: dict[str, str] = {}
    for line in m.group("hdr").splitlines():
        hm = _HDR_LINE.match(line)
        if hm:
            hdrs[hm.group(1).lower()] = hm.group(2).strip()
    if "from" not in hdrs:
        return None
    note = body[: m.start()].strip()
    original = body[m.end() :]
    return hdrs, note, original


def parse_eml(raw: bytes) -> ParsedMessage:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return _parse_message(msg)


def _parse_message(msg: EmailMessage, depth: int = 0) -> ParsedMessage:
    text, html, attachments, rfc822 = _body_parts(msg)
    if not text and html:
        text = html_to_text(html)
    sent_at, conf = _decode_date(msg)
    from_pairs = getaddresses(msg.get_all("From", []))
    from_name, from_addr = from_pairs[0] if from_pairs else ("", "")
    subject = str(msg.get("Subject", "") or "")
    headers = {k: str(v) for k, v in msg.items()}
    refs = [r for r in str(msg.get("References", "") or "").split() if r]

    parsed = ParsedMessage(
        internet_message_id=(msg.get("Message-ID") or None) and str(msg.get("Message-ID")).strip(),
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
    )

    is_forward = bool(_FWD_SUBJECT.match(subject)) or bool(rfc822)
    if not is_forward or depth > 3:
        return parsed

    # Case 1: attached original as message/rfc822
    if rfc822:
        inner = _parse_message(rfc822[0], depth + 1)
        inner.forward_chain = [parsed.from_addr, *inner.forward_chain]
        inner.forwarded_by = inner.forwarded_by or parsed.from_addr
        inner.forward_note = parsed.body_trimmed or None
        inner.attachments = inner.attachments or attachments
        return inner

    # Case 2: inline forward with header block in the text
    inline = _parse_inline_forward(text)
    if inline:
        hdrs, note, original = inline
        orig_from = getaddresses([hdrs.get("from", "")])
        o_name, o_addr = orig_from[0] if orig_from else ("", "")
        o_date = None
        for key in ("sent", "date"):
            if key in hdrs:
                try:
                    o_date = parsedate_to_datetime(hdrs[key])
                except (TypeError, ValueError):
                    try:
                        from dateutil import parser as dparser

                        o_date = dparser.parse(hdrs[key])
                    except (ValueError, OverflowError):
                        o_date = None
                break
        if o_date is not None and o_date.tzinfo is None:
            from bidtriage.core.clock import BUSINESS_TZ

            o_date = o_date.replace(tzinfo=BUSINESS_TZ)
        nested = _parse_inline_forward(original)
        if nested:
            # FW: FW: — recurse by constructing a pseudo message
            inner_hdrs, _, inner_body = nested
            inner_from = getaddresses([inner_hdrs.get("from", "")])
            i_name, i_addr = inner_from[0] if inner_from else ("", "")
            return ParsedMessage(
                internet_message_id=None,
                from_addr=i_addr.lower(),
                from_name=i_name,
                to=parsed.to,
                cc=[],
                subject=_FWD_SUBJECT.sub("", _FWD_SUBJECT.sub("", subject)).strip(),
                sent_at=o_date,
                sent_at_confidence="low",
                body_text=inner_body,
                body_html="",
                body_trimmed=trim_quoted(inner_body),
                headers=headers,
                in_reply_to=None,
                references=refs,
                attachments=attachments,
                forwarded_by=parsed.from_addr,
                forward_note=note or None,
                forward_chain=[parsed.from_addr, o_addr.lower()],
            )
        return ParsedMessage(
            internet_message_id=None,
            from_addr=o_addr.lower(),
            from_name=o_name,
            to=parsed.to,
            cc=[],
            subject=_FWD_SUBJECT.sub("", hdrs.get("subject", subject)).strip(),
            sent_at=o_date or sent_at,
            sent_at_confidence="low" if o_date else conf,
            body_text=original,
            body_html="",
            body_trimmed=trim_quoted(original),
            headers=headers,
            in_reply_to=None,
            references=refs,
            attachments=attachments,
            forwarded_by=parsed.from_addr,
            forward_note=note or None,
            forward_chain=[parsed.from_addr],
        )
    return parsed
