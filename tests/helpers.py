"""Builders for the attachment types SPEC-01 F5 has to handle, so tests use real files."""

from __future__ import annotations

import io
import zipfile

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def tiny_pdf(*pages: str) -> bytes:
    """A minimal single- or multi-page PDF with a real text layer."""
    objs: list[bytes] = []
    page_ids = [3 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_ids[i] + 1} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _assemble_pdf(objs)


def blank_pdf(pages: int = 2) -> bytes:
    """Pages with no text layer at all: what a scanner produces (OCR path)."""
    return tiny_pdf(*[""] * pages)


def _assemble_pdf(objs: list[bytes]) -> bytes:
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


def encrypted_pdf(password: str = "secret") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(tiny_pdf("Confidential bid form"))).pages:
        writer.add_page(page)
    writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def docx_bytes(*paragraphs: str) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx_bytes(sheets: dict[str, list[list[str]]]) -> bytes:
    """A hand-built XLSX with inline strings; enough to exercise the sheet-XML reader."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        names = list(sheets)
        entries = "".join(
            f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, start=1)
        )
        z.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{SHEET_NS}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{entries}</sheets></workbook>",
        )
        for i, name in enumerate(names, start=1):
            rows = "".join(
                "<row>"
                + "".join(f'<c t="inlineStr"><is><t>{cell}</t></is></c>' for cell in row)
                + "</row>"
                for row in sheets[name]
            )
            z.writestr(
                f"xl/worksheets/sheet{i}.xml",
                f'<?xml version="1.0"?><worksheet xmlns="{SHEET_NS}"><sheetData>{rows}</sheetData></worksheet>',
            )
    return buf.getvalue()


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def drain(fetch_session, *, fails: set[str] | None = None) -> tuple[list[str], dict]:
    """Iterate a FetchSession the way run_poll does: store each message, report the outcome.

    `fails` names provider ids whose *store* should fail, so tests can exercise cursor safety
    without a database.
    """
    fails = fails or set()
    stored: list[str] = []
    for item in fetch_session:
        pid = item.provider_message_id
        ok = pid not in fails
        fetch_session.record(pid, stored=ok, error=None if ok else "store failed")
        if ok:
            stored.append(pid)
    return stored, fetch_session.new_state()
