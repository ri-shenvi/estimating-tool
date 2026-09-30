"""Outlook `.msg` reading for manual upload (SPEC-01 F1).

Compound-file parsing itself is olefile's job; what is tested here is the MAPI property decoding
and the reassembly into a ParsedMessage, driven through a stand-in for an open compound file.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from bidtriage.ingestion.msg import is_msg, parse_ole, parse_upload
from tests.helpers import tiny_pdf

FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


def _filetime(dt: datetime) -> bytes:
    ticks = int((dt - FILETIME_EPOCH) / timedelta(microseconds=1)) * 10
    return ticks.to_bytes(8, "little")


class FakeOle:
    """Minimal stand-in for olefile.OleFileIO: a flat map of stream path -> bytes."""

    def __init__(self, streams: dict[tuple[str, ...], bytes]) -> None:
        self.streams = streams

    def listdir(self, streams: bool = True, storages: bool = False) -> list[list[str]]:
        return [list(path) for path in self.streams]

    def openstream(self, filename: object) -> io.BytesIO:
        key = tuple(filename) if isinstance(filename, list) else (str(filename),)
        return io.BytesIO(self.streams[key])


def _unicode(value: str) -> bytes:
    return value.encode("utf-16-le")


def test_parse_msg_without_transport_headers():
    ole = FakeOle(
        {
            ("__substg1.0_0037001F",): _unicode("ITB - Baldwin HS Auditorium"),
            ("__substg1.0_1000001F",): _unicode("Bids due 10/20 at 2 PM.\r\nPrevailing wage."),
            ("__substg1.0_0C1F001F",): _unicode("bbuilder@mascaroconstruction.com"),
            ("__substg1.0_0C1A001F",): _unicode("Bob Builder"),
            ("__substg1.0_0E04001F",): _unicode("Dana Estimator <dana@ferryelectric.com>"),
            ("__substg1.0_0E03001F",): _unicode("casey@ferryelectric.com"),
            ("__substg1.0_1035001F",): _unicode("<msg-1@mascaroconstruction.com>"),
            ("__substg1.0_00390040",): _filetime(datetime(2026, 9, 29, 20, 45, tzinfo=UTC)),
            ("__attach_version1.0_#00000000", "__substg1.0_3707001F"): _unicode("ITB Letter.pdf"),
            ("__attach_version1.0_#00000000", "__substg1.0_370E001F"): _unicode("application/pdf"),
            ("__attach_version1.0_#00000000", "__substg1.0_37010102"): tiny_pdf("Bids due 10/20"),
        }
    )
    m = parse_ole(ole)
    assert m.subject == "ITB - Baldwin HS Auditorium"
    assert m.from_addr == "bbuilder@mascaroconstruction.com" and m.from_name == "Bob Builder"
    assert m.to == ["dana@ferryelectric.com"] and m.cc == ["casey@ferryelectric.com"]
    assert m.internet_message_id == "<msg-1@mascaroconstruction.com>"
    assert m.sent_at == datetime(2026, 9, 29, 20, 45, tzinfo=UTC)
    assert m.sent_at_confidence == "high"
    assert "Prevailing wage." in m.body_text and m.body_trimmed.startswith("Bids due")
    assert [(a.filename, a.mime, len(a.data) > 0) for a in m.attachments] == [
        ("ITB Letter.pdf", "application/pdf", True)
    ]


def test_parse_msg_prefers_original_transport_headers():
    """When Outlook kept the RFC 822 headers, Message-ID and the date come from them."""
    headers = (
        "Message-ID: <original@pjdick.com>\r\n"
        "From: Pat Planner <pat@pjdick.com>\r\n"
        "To: estimating@ferryelectric.com\r\n"
        "Subject: ITB - Shaler Middle School\r\n"
        "Date: Tue, 29 Sep 2026 14:30:00 -0400\r\n"
        "References: <thread-1@pjdick.com>\r\n"
    )
    ole = FakeOle(
        {
            ("__substg1.0_007D001F",): _unicode(headers),
            ("__substg1.0_0037001F",): _unicode("ITB - Shaler Middle School"),
            ("__substg1.0_1000001F",): _unicode("Bids due 10/14 at 11 AM."),
        }
    )
    m = parse_ole(ole)
    assert m.internet_message_id == "<original@pjdick.com>"
    assert m.from_addr == "pat@pjdick.com" and m.references == ["<thread-1@pjdick.com>"]
    assert m.sent_at is not None and m.sent_at.astimezone(UTC).hour == 18
    assert m.body_text == "Bids due 10/14 at 11 AM."
    assert m.sent_at_confidence == "high"


def test_cp1252_properties_and_missing_sender_are_tolerated():
    ole = FakeOle(
        {
            ("__substg1.0_0037001E",): b"ITB \x96 Caf\xe9 Renovation",
            ("__substg1.0_1000001E",): b"Bids due 10/20.",
            ("__substg1.0_0065001F",): _unicode("Bob Builder <bbuilder@mascaroconstruction.com>"),
        }
    )
    m = parse_ole(ole)
    assert m.subject == "ITB – Café Renovation"
    assert m.from_addr == "bbuilder@mascaroconstruction.com"
    assert m.sent_at is None and m.sent_at_confidence == "low"


def test_is_msg_and_parse_upload_dispatch():
    eml = b"Message-ID: <e@x>\r\nFrom: gc@example-gc.com\r\nSubject: ITB\r\n\r\nBody.\r\n"
    assert not is_msg(eml)
    assert parse_upload("letter.eml", eml).internet_message_id == "<e@x>"
    assert is_msg(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40)
