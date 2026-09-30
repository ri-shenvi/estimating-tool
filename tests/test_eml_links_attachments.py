from bidtriage.ingestion import classify_host, harvest_links, parse_eml
from bidtriage.ingestion.attachments import extract, extract_text, sanitize_filename, sniff_mime
from bidtriage.ingestion.links import unwrap
from tests.helpers import (
    blank_pdf,
    docx_bytes,
    encrypted_pdf,
    tiny_pdf,
    xlsx_bytes,
    zip_bytes,
)


def test_parse_html_only(fixtures_dir):  # type: ignore[no-untyped-def]
    m = parse_eml((fixtures_dir / "bc_invite_benedum.eml").read_bytes())
    assert m.from_addr == "team@buildingconnected.com" and "PJ Dick" in m.from_name
    assert "Bid Due" in m.body_text and "buildingconnected.com/projects" in m.body_text
    assert m.internet_message_id == "<bc-8f3a2b1c@buildingconnected.com>"


def test_quoted_history_trimmed(fixtures_dir):  # type: ignore[no-untyped-def]
    m = parse_eml((fixtures_dir / "gc_email_benedum.eml").read_bytes())
    assert "Original Message" not in m.body_trimmed and "Bids are due" in m.body_trimmed
    assert m.cc == ["casey@ferryelectric.com", "dana@ferryelectric.com"]


def test_inline_forward_unwrapped(fixtures_dir):  # type: ignore[no-untyped-def]
    m = parse_eml((fixtures_dir / "forward_inline_mascaro.eml").read_bytes())
    assert m.from_addr == "bbuilder@mascaroconstruction.com"
    assert m.forwarded_by == "dana@ferryelectric.com"
    assert m.forward_note == "Casey, worth a look? Mascaro job, sounds like our kind of thing."
    assert m.sent_at.day == 29 and m.subject.startswith("ITB - AHN Wexford")


def test_rfc822_forward():  # type: ignore[no-untyped-def]
    raw = b"""Message-ID: <outer@x>\r\nFrom: Dana <dana@ferryelectric.com>\r\nTo: estimating@ferryelectric.com\r\nSubject: FW: ITB Test\r\nDate: Wed, 30 Sep 2026 08:00:00 -0400\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"B\"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nFYI\r\n--B\r\nContent-Type: message/rfc822\r\n\r\nMessage-ID: <inner@gc>\r\nFrom: GC <gc@example-gc.com>\r\nTo: dana@ferryelectric.com\r\nSubject: ITB Test\r\nDate: Tue, 29 Sep 2026 15:00:00 -0400\r\nContent-Type: text/plain\r\n\r\nBids due 10/20 at 2 PM.\r\n--B--\r\n"""
    m = parse_eml(raw)
    assert m.from_addr == "gc@example-gc.com" and m.internet_message_id == "<inner@gc>"
    assert m.forwarded_by == "dana@ferryelectric.com" and m.forward_note == "FYI"


def test_content_hash_ignores_fw_prefix_and_missing_message_id():  # type: ignore[no-untyped-def]
    a = parse_eml(
        b"From: a@b.com\r\nSubject: Hello\r\nDate: Wed, 30 Sep 2026 08:00:00 -0400\r\n\r\nBody text\r\n"
    )
    b = parse_eml(
        b"From: a@b.com\r\nSubject: FW: Hello\r\nDate: Wed, 30 Sep 2026 09:00:00 -0400\r\n\r\nBody text\r\n"
    )
    assert a.internet_message_id is None and a.content_hash == b.content_hash


def test_bad_date_header():  # type: ignore[no-untyped-def]
    m = parse_eml(b"From: a@b.com\r\nSubject: x\r\nDate: not a date\r\n\r\nbody\r\n")
    assert m.sent_at is None and m.sent_at_confidence == "low"


def test_links_unwrap_and_classify():  # type: ignore[no-untyped-def]
    wrapped = "https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fapp.buildingconnected.com%2Fprojects%2Fabc%2Frfp&data=05"
    url, still = unwrap(wrapped)
    assert url == "https://app.buildingconnected.com/projects/abc/rfp" and not still
    links = harvest_links(f"see {wrapped} and https://mascaro.egnyte.com/fl/X.")
    assert [lk["host_class"] for lk in links] == ["buildingconnected", "egnyte"]
    assert classify_host("https://procore.com/123/project/bidding") == "procore"
    assert classify_host("https://example.com") == "other"


def test_sanitize_and_sniff():  # type: ignore[no-untyped-def]
    assert sanitize_filename("../evil\x00.pdf") == ".._evil_.pdf"
    assert sniff_mime(b"%PDF-1.4 ...", "application/octet-stream") == "application/pdf"
    assert sniff_mime(b"<html><body>redirect</body></html>", "application/pdf") == "text/html"
    assert extract_text("x.pdf", "application/pdf", b"<html>not a pdf</html>").text is None


def test_pdf_text_extraction():  # type: ignore[no-untyped-def]
    pdf = tiny_pdf("Bids due October 16 at 2 PM")
    out = extract_text("ITB.pdf", "application/pdf", pdf)
    assert out.text and "October 16" in out.text and out.pages == 1 and not out.large_document


def test_oversize_attachment():  # type: ignore[no-untyped-def]
    class Big(bytes):
        def __len__(self) -> int:
            return 201 * 1024 * 1024

    assert extract_text("big.pdf", "application/pdf", Big(b"%PDF")).error == "oversize"


# ------------------------------------------------------------ SPEC-01 edge cases


def test_html_only_body():  # type: ignore[no-untyped-def]
    raw = (
        b"Message-ID: <html-only@gc.com>\r\nFrom: GC <gc@example-gc.com>\r\n"
        b"To: estimating@ferryelectric.com\r\nSubject: ITB\r\n"
        b"Date: Wed, 30 Sep 2026 08:00:00 -0400\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
        b"<html><body><p>Bids due 10/20.</p>"
        b'<p><a href="https://app.buildingconnected.com/projects/abc">Plan room</a></p>'
        b"</body></html>\r\n"
    )
    m = parse_eml(raw)
    assert "Bids due 10/20." in m.body_text
    assert "https://app.buildingconnected.com/projects/abc" in m.body_text
    assert [lk["host_class"] for lk in harvest_links(m.body_text)] == ["buildingconnected"]


def test_missing_message_id():  # type: ignore[no-untyped-def]
    m = parse_eml(
        b"From: gc@example-gc.com\r\nSubject: ITB Acme\r\n"
        b"Date: Wed, 30 Sep 2026 08:00:00 -0400\r\n\r\nBids due 10/20.\r\n"
    )
    assert m.internet_message_id is None
    assert len(m.content_hash) == 64


def test_hash_includes_attachments():  # type: ignore[no-untyped-def]
    """Same subject and body, different attachments: two records, not one (SPEC-01 F3)."""
    a = _with_attachment("ITB.pdf", b"%PDF-1.4 first")
    b = _with_attachment("ITB.pdf", b"%PDF-1.4 second")
    assert a.content_hash != b.content_hash
    assert _with_attachment("ITB.pdf", b"%PDF-1.4 first").content_hash == a.content_hash


def test_nested_forward():  # type: ignore[no-untyped-def]
    raw = (
        b"Message-ID: <fw2@ferryelectric.com>\r\nFrom: Casey Chief <casey@ferryelectric.com>\r\n"
        b"To: estimating@ferryelectric.com\r\nSubject: FW: FW: ITB - Baldwin HS\r\n"
        b"Date: Wed, 30 Sep 2026 09:00:00 -0400\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        b"Dana, please log this one.\r\n\r\n"
        b"-----Original Message-----\r\n"
        b"From: Dana Estimator <dana@ferryelectric.com>\r\n"
        b"Sent: Tuesday, September 29, 2026 5:10 PM\r\n"
        b"To: Casey Chief <casey@ferryelectric.com>\r\n"
        b"Subject: FW: ITB - Baldwin HS\r\n\r\n"
        b"Casey, worth a look?\r\n\r\n"
        b"-----Original Message-----\r\n"
        b"From: Bob Builder <bbuilder@mascaroconstruction.com>\r\n"
        b"Sent: Tuesday, September 29, 2026 4:45 PM\r\n"
        b"To: Dana Estimator <dana@ferryelectric.com>\r\n"
        b"Subject: ITB - Baldwin HS\r\n\r\n"
        b"Bids due 10/20 at 2 PM.\r\n"
    )
    m = parse_eml(raw)
    assert m.from_addr == "bbuilder@mascaroconstruction.com"
    assert m.subject == "ITB - Baldwin HS"
    assert "Bids due 10/20 at 2 PM." in m.body_text
    # Each forwarder, outermost first; the innermost original becomes the message itself.
    assert m.forward_chain == ["casey@ferryelectric.com", "dana@ferryelectric.com"]
    assert m.forwarded_by == "casey@ferryelectric.com"
    assert m.forward_note == "Dana, please log this one."
    assert m.sent_at is not None and m.sent_at.hour == 16 and m.sent_at.minute == 45


def test_inline_forward_headers():  # type: ignore[no-untyped-def]
    """Outlook sometimes inlines the original with a bare header block and no separator line."""
    raw = (
        b"Message-ID: <bare@ferryelectric.com>\r\nFrom: Dana <dana@ferryelectric.com>\r\n"
        b"To: estimating@ferryelectric.com\r\nSubject: Fwd: ITB - Shaler Middle School\r\n"
        b"Date: Wed, 30 Sep 2026 08:15:00 -0400\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        b"Take a look.\r\n\r\n"
        b"From: Pat Planner <pat@pjdick.com>\r\n"
        b"Sent: Tuesday, September 29, 2026 2:30 PM\r\n"
        b"To: Dana <dana@ferryelectric.com>\r\n"
        b"Subject: ITB - Shaler Middle School\r\n\r\n"
        b"Bids due 10/14 at 11 AM. Prevailing wage.\r\n"
    )
    m = parse_eml(raw)
    assert m.from_addr == "pat@pjdick.com" and m.from_name == "Pat Planner"
    assert m.subject == "ITB - Shaler Middle School"
    assert m.forwarded_by == "dana@ferryelectric.com"
    assert m.forward_note == "Take a look."
    assert "Bids due 10/14 at 11 AM." in m.body_text
    assert m.sent_at is not None and m.sent_at.day == 29 and m.sent_at.hour == 14


def test_quoted_history_trim():  # type: ignore[no-untyped-def]
    """The whole body is kept; body_trimmed stops at the first quoted-reply marker."""
    history = "\n".join(f"line {i} of quoted history" for i in range(200))
    raw = (
        "Message-ID: <trim@gc.com>\r\nFrom: gc@example-gc.com\r\nSubject: RE: ITB\r\n"
        "Date: Wed, 30 Sep 2026 08:00:00 -0400\r\n\r\n"
        "Addendum 2 is posted.\r\n\r\n"
        "-----Original Message-----\r\n" + history + "\r\n"
    ).encode()
    m = parse_eml(raw)
    assert m.body_trimmed == "Addendum 2 is posted."
    assert "line 199 of quoted history" in m.body_text
    assert len(m.body_text) > len(m.body_trimmed)


def test_charsets():  # type: ignore[no-untyped-def]
    subject = "=?windows-1252?Q?ITB_=96_Caf=E9_Renovation?="
    body = "Bids due 10/20 — pre-bid ☑ mandatory".encode()
    raw = (
        f"Message-ID: <cs@gc.com>\r\nFrom: gc@example-gc.com\r\nSubject: {subject}\r\n"
        "Date: Wed, 30 Sep 2026 08:00:00 -0400\r\nMIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
    ).encode() + body
    m = parse_eml(raw)
    assert m.subject == "ITB – Café Renovation"
    assert "—" in m.body_text and "☑" in m.body_text


def test_filename_sanitize():  # type: ignore[no-untyped-def]
    assert sanitize_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert sanitize_filename("ITB\x00.pdf") == "ITB_.pdf"
    assert sanitize_filename("   ") == "attachment"
    m = _with_attachment("../../evil\x00.pdf", b"%PDF-1.4 x")
    assert sanitize_filename(m.attachments[0].filename) == ".._.._evil_.pdf"


def test_mime_sniff():  # type: ignore[no-untyped-def]
    """An attachment named .pdf whose content is an HTML portal redirect is treated as other."""
    html = b"<html><body>Redirecting to the plan room...</body></html>"
    out = extract("ITB_Invitation.pdf", "application/pdf", html)
    assert out.mime == "text/html" and out.text is None and out.error is None
    assert sniff_mime(docx_bytes("x"), "application/octet-stream").endswith(
        "wordprocessingml.document"
    )
    assert sniff_mime(zip_bytes({"a.txt": b"x"}), "application/pdf") == "application/zip"


def test_encrypted_pdf():  # type: ignore[no-untyped-def]
    out = extract("Sealed.pdf", "application/pdf", encrypted_pdf())
    assert out.text is None and out.error == "encrypted"
    assert out.mime == "application/pdf" and not out.large_document


def test_large_drawing_set_not_extracted():  # type: ignore[no-untyped-def]
    """A drawing set is stored but never text-extracted (SPEC-01 F5)."""
    out = extract("Drawings.pdf", "application/pdf", tiny_pdf(*["sheet"] * 45))
    assert out.text is None and out.large_document and out.pages == 45 and not out.oversize


def test_ocr_for_scanned_pdf():  # type: ignore[no-untyped-def]
    class FakeOcr:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def page_texts(self, pdf: bytes, max_pages: int) -> list[str]:
            self.calls.append(max_pages)
            return ["INVITATION TO BID", "Bids due October 20"]

    backend = FakeOcr()
    out = extract("Scan.pdf", "application/pdf", blank_pdf(2), ocr=backend)
    assert out.ocr and out.text is not None
    assert "INVITATION TO BID" in out.text and "[page 2]" in out.text
    assert backend.calls == [2] and out.error is None


def test_ocr_failure_is_not_fatal():  # type: ignore[no-untyped-def]
    class Broken:
        def page_texts(self, pdf: bytes, max_pages: int) -> list[str]:
            raise RuntimeError("tesseract died")

    out = extract("Scan.pdf", "application/pdf", blank_pdf(1), ocr=Broken())
    assert out.text is None and out.error == "ocr_failed" and not out.ocr


def test_zip_extracts_members_and_lists_nested():  # type: ignore[no-untyped-def]
    payload = zip_bytes(
        {
            "ITB Letter.pdf": tiny_pdf("Bids due 10/20"),
            "Scope.pdf": tiny_pdf("Division 26 scope"),
            "Bid Form.pdf": tiny_pdf("Base bid $"),
            "Drawings.zip": zip_bytes({"E1.pdf": tiny_pdf("do not open")}),
        }
    )
    out = extract("Bid Package.zip", "application/zip", payload)
    assert out.mime == "application/zip"
    assert sorted(m.filename for m in out.members) == [
        "Bid Form.pdf",
        "ITB Letter.pdf",
        "Scope.pdf",
    ]
    assert out.text is not None
    assert "Drawings.zip" in out.text and "nested archive not opened" in out.text
    assert "do not open" not in out.text


def test_xlsx_limits():  # type: ignore[no-untyped-def]
    sheets = {f"S{i}": [[f"r{r}", "x"] for r in range(600)] for i in range(7)}
    out = extract("Bid Form.xlsx", "application/octet-stream", xlsx_bytes(sheets))
    assert out.text is not None
    assert out.text.count("[sheet ") == 5  # first 5 sheets only
    assert out.text.count("\t") == 5 * 500  # 500 rows per sheet


def test_docx_extraction():  # type: ignore[no-untyped-def]
    out = extract("Scope.docx", "application/octet-stream", docx_bytes("Division 26", "Bids 10/20"))
    assert out.text is not None and "Division 26" in out.text and "Bids 10/20" in out.text


def test_inline_signature_image_ignored():  # type: ignore[no-untyped-def]
    raw = (
        b"Message-ID: <sig@gc.com>\r\nFrom: gc@example-gc.com\r\nTo: estimating@ferryelectric.com\r\n"
        b"Subject: ITB\r\nDate: Wed, 30 Sep 2026 08:00:00 -0400\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/related; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nBids due 10/20.\r\n"
        b"--B\r\nContent-Type: image/png\r\nContent-ID: <logo>\r\n"
        b'Content-Disposition: inline; filename="image001.png"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="ITB.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n"
        b"--B--\r\n"
    )
    m = parse_eml(raw)
    assert [a.filename for a in m.attachments] == ["ITB.pdf"]


def test_links_from_attachment_text_and_wrapped_flag():  # type: ignore[no-untyped-def]
    attachment_text = (
        "[page 1]\nPlan room: https://mascaro.egnyte.com/fl/AHNWexford\n"
        "Tracking: https://click.example.net/x/abc123\n"
    )
    links = harvest_links("Details attached.", attachment_text)
    by_class = {str(lk["host_class"]): lk for lk in links}
    assert by_class["egnyte"]["wrapped"] is False
    assert by_class["other"]["wrapped"] is True


def test_received_at_from_trace_header():  # type: ignore[no-untyped-def]
    """A malformed Date falls back to the server's Received time (SPEC-01 F2)."""
    raw = (
        b"Received: from mail.gc.com by ferry.mail with SMTP id X;\r\n"
        b"\tTue, 29 Sep 2026 16:45:12 -0400\r\n"
        b"Message-ID: <trace@gc.com>\r\nFrom: gc@example-gc.com\r\nSubject: ITB\r\n"
        b"Date: garbage\r\n\r\nBids due 10/20.\r\n"
    )
    m = parse_eml(raw)
    assert m.sent_at is None and m.sent_at_confidence == "low"
    assert m.received_at is not None and m.received_at.day == 29


def _with_attachment(filename: str, data: bytes):  # type: ignore[no-untyped-def]
    import base64

    encoded = base64.b64encode(data).decode()
    raw = (
        "Message-ID: <att@gc.com>\r\nFrom: gc@example-gc.com\r\nSubject: ITB Acme\r\n"
        "Date: Wed, 30 Sep 2026 08:00:00 -0400\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
        f'--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="{filename}"\r\n'
        f"Content-Transfer-Encoding: base64\r\n\r\n{encoded}\r\n--B--\r\n"
    ).encode()
    return parse_eml(raw)
