from bidtriage.ingestion import classify_host, harvest_links, parse_eml
from bidtriage.ingestion.attachments import extract_text, sanitize_filename, sniff_mime
from bidtriage.ingestion.links import unwrap


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
    pdf = _tiny_pdf("Bids due October 16 at 2 PM")
    out = extract_text("ITB.pdf", "application/pdf", pdf)
    assert out.text and "October 16" in out.text and out.pages == 1 and not out.large_document


def test_oversize_attachment():  # type: ignore[no-untyped-def]
    class Big(bytes):
        def __len__(self) -> int:
            return 201 * 1024 * 1024

    assert extract_text("big.pdf", "application/pdf", Big(b"%PDF")).error == "oversize"


def _tiny_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
