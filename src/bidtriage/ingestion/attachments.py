"""Attachment text extraction (SPEC-01 F5).

`extract` is the single entry point: it sniffs the real MIME type, extracts text where the table in
SPEC-01 F5 says to, and expands containers (ZIP) into member attachments. OCR is injected so the
default (Tesseract, an optional extra) is never required by tests.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Protocol

LARGE_PAGES = 40
LARGE_BYTES = 50 * 1024 * 1024
OVERSIZE_BYTES = 200 * 1024 * 1024
ZIP_MAX_FILES = 25
ZIP_MAX_TOTAL = 200 * 1024 * 1024
ZIP_MAX_LISTED = 1000
"""Cap on listed entries: an archive with a million names must not become a million-line text."""
OCR_MAX_PAGES = 20
OCR_TIMEOUT_SECONDS = 60
XLSX_MAX_SHEETS = 5
XLSX_MAX_ROWS = 500
_UNSAFE = re.compile(r"[\\/\x00-\x1f]")
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class OcrBackend(Protocol):
    def page_texts(self, pdf: bytes, max_pages: int) -> list[str]: ...


@dataclass
class Member:
    """A file pulled out of a container attachment."""

    filename: str
    data: bytes


@dataclass
class ExtractedText:
    text: str | None
    mime: str = "application/octet-stream"
    ocr: bool = False
    large_document: bool = False
    oversize: bool = False
    error: str | None = None
    pages: int | None = None
    members: list[Member] = field(default_factory=list)


def sanitize_filename(name: str) -> str:
    """Strip path separators and control bytes so a name can never escape the blob root."""
    return _UNSAFE.sub("_", name).strip() or "attachment"


def sniff_mime(data: bytes, declared: str, filename_hint: str = "") -> str:
    """Trust magic bytes over the declared type: portals send HTML redirects named `.pdf`."""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return _sniff_ooxml(data, declared)
    if head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "text/html"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # An OLE compound file: a .msg, or a legacy .doc/.xls we do not text-extract.
        return "application/vnd.ms-outlook" if filename_hint.endswith(".msg") else declared
    return declared


def _sniff_ooxml(data: bytes, declared: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return "application/zip"
    if "word/document.xml" in names:
        return DOCX_MIME
    if "xl/workbook.xml" in names:
        return XLSX_MIME
    return "application/zip"


class TesseractOcr:
    def page_texts(self, pdf: bytes, max_pages: int) -> list[str]:
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(pdf, dpi=200, first_page=1, last_page=max_pages)
        return [pytesseract.image_to_string(img, timeout=OCR_TIMEOUT_SECONDS) for img in images]


def _has_text_layer(text: str, pages: int) -> bool:
    return len(re.sub(r"\W", "", text)) >= 20 * max(pages, 1)


def extract_pdf_text(data: bytes, *, ocr: OcrBackend | None = None) -> ExtractedText:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return ExtractedText(None, mime="application/pdf", error="encrypted")
            except Exception:  # noqa: BLE001 - pypdf raises several unrelated types here
                return ExtractedText(None, mime="application/pdf", error="encrypted")
        pages = len(reader.pages)
    except Exception:  # noqa: BLE001 - pypdf raises PdfReadError, KeyError, struct.error, ...
        return ExtractedText(None, mime="application/pdf", error="unreadable")
    if pages > LARGE_PAGES or len(data) > LARGE_BYTES:
        return ExtractedText(None, mime="application/pdf", large_document=True, pages=pages)
    chunks = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            t = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a single broken page must not lose the document
            t = ""
        chunks.append(f"[page {i}]\n{t.strip()}")
    text = "\n\n".join(chunks)
    if _has_text_layer(text, pages):
        return ExtractedText(text, mime="application/pdf", pages=pages)
    if ocr is not None:
        return _ocr_pdf(data, pages, ocr)
    return ExtractedText(text or None, mime="application/pdf", error="no_text_layer", pages=pages)


def _ocr_pdf(data: bytes, pages: int, backend: OcrBackend) -> ExtractedText:
    try:
        texts = backend.page_texts(data, min(pages, OCR_MAX_PAGES))
    except ImportError:
        return ExtractedText(None, mime="application/pdf", error="ocr_unavailable", pages=pages)
    except Exception:  # noqa: BLE001 - SPEC-01: OCR failure is non-fatal
        return ExtractedText(None, mime="application/pdf", error="ocr_failed", pages=pages)
    text = "\n\n".join(f"[page {i}]\n{t.strip()}" for i, t in enumerate(texts, start=1))
    return ExtractedText(text, mime="application/pdf", ocr=True, pages=pages)


def extract_docx_text(data: bytes) -> ExtractedText:
    try:
        import docx
    except ImportError:  # pragma: no cover - python-docx is a hard dependency
        return ExtractedText(None, mime=DOCX_MIME, error="docx_unavailable")
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return ExtractedText(None, mime=DOCX_MIME, error="unreadable")
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append("\t".join(c.text.strip() for c in row.cells))
    return ExtractedText("\n".join(parts), mime=DOCX_MIME)


def extract_xlsx_text(data: bytes) -> ExtractedText:
    """Read the sheet XML directly: bid forms are simple grids and this avoids a heavy dependency."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            shared = _xlsx_shared_strings(z)
            sheets = sorted(
                n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
            )
            out: list[str] = []
            for name in sheets[:XLSX_MAX_SHEETS]:
                out.append(f"[sheet {name.rsplit('/', 1)[-1].removesuffix('.xml')}]")
                out.extend(_xlsx_rows(z.read(name), shared))
    except (zipfile.BadZipFile, ET.ParseError):
        return ExtractedText(None, mime=XLSX_MIME, error="unreadable")
    return ExtractedText("\n".join(out), mime=XLSX_MIME)


def _xlsx_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{_SHEET_NS}t")) for si in root]


def _xlsx_rows(sheet_xml: bytes, shared: list[str]) -> list[str]:
    root = ET.fromstring(sheet_xml)
    rows: list[str] = []
    for row in root.iter(f"{_SHEET_NS}row"):
        if len(rows) >= XLSX_MAX_ROWS:
            break
        cells = []
        for c in row.iter(f"{_SHEET_NS}c"):
            v = c.find(f"{_SHEET_NS}v")
            raw = v.text or "" if v is not None else ""
            if c.get("t") == "s" and raw.isdigit() and int(raw) < len(shared):
                raw = shared[int(raw)]
            elif c.get("t") == "inlineStr":
                is_el = c.find(f"{_SHEET_NS}is")
                raw = (
                    "".join(t.text or "" for t in is_el.iter(f"{_SHEET_NS}t"))
                    if is_el is not None
                    else ""
                )
            cells.append(raw)
        if any(cells):
            rows.append("\t".join(cells))
    return rows


def extract_zip(data: bytes) -> ExtractedText:
    """List every entry; hand back only the PDF/DOCX/XLSX members for text extraction.

    Nested archives are listed but never opened (SPEC-01 F5: deeper nesting ignored).
    """
    listing: list[str] = []
    members: list[Member] = []
    total = 0
    omitted = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                if len(listing) >= ZIP_MAX_LISTED:
                    omitted += 1
                    continue
                name = info.filename
                lower = name.lower()
                if lower.endswith((".zip", ".7z", ".rar")):
                    listing.append(f"{name} ({info.file_size} bytes, nested archive not opened)")
                    continue
                listing.append(f"{name} ({info.file_size} bytes)")
                if not lower.endswith((".pdf", ".docx", ".xlsx")):
                    continue
                if len(members) >= ZIP_MAX_FILES or total >= ZIP_MAX_TOTAL:
                    continue
                # `info.file_size` comes from the central directory and a hostile archive can
                # understate it, so the cap is enforced while reading rather than before
                # (SPEC-10 F9).
                payload = _read_limited(z, info, ZIP_MAX_TOTAL - total)
                if payload is None:
                    listing[-1] = f"{name} (skipped: exceeds the {ZIP_MAX_TOTAL} byte budget)"
                    continue
                total += len(payload)
                members.append(Member(sanitize_filename(name.rsplit("/", 1)[-1]), payload))
    except zipfile.BadZipFile:
        return ExtractedText(None, mime="application/zip", error="unreadable")
    if omitted:
        listing.append(f"... and {omitted} more entries not listed")
    return ExtractedText("\n".join(listing) or None, mime="application/zip", members=members)


def _read_limited(z: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes | None:
    """Decompress one member, giving up if it exceeds `budget` regardless of its declared size."""
    if budget <= 0:
        return None
    with z.open(info) as fh:
        payload = fh.read(budget + 1)
    return None if len(payload) > budget else payload


def list_zip(data: bytes) -> list[tuple[str, bytes]]:
    """Back-compat shim: the extractable members of a ZIP as (filename, bytes)."""
    return [(m.filename, m.data) for m in extract_zip(data).members]


def extract(
    filename: str, mime: str, data: bytes, *, ocr: OcrBackend | None = None
) -> ExtractedText:
    """Extract text for one attachment. Never raises: a bad file yields an `error` instead."""
    if len(data) > OVERSIZE_BYTES:
        return ExtractedText(None, mime=mime, oversize=True, error="oversize")
    lower = filename.lower()
    sniffed = sniff_mime(data, mime, lower)
    if sniffed == "application/pdf":
        return extract_pdf_text(data, ocr=ocr)
    if sniffed == DOCX_MIME:
        return extract_docx_text(data)
    if sniffed == XLSX_MIME:
        return extract_xlsx_text(data)
    if sniffed == "application/zip":
        return extract_zip(data)
    if sniffed == "text/html":
        # A portal redirect page masquerading as a document: treated as `other` (SPEC-01 F5).
        return ExtractedText(None, mime=sniffed)
    if sniffed.startswith("text/") or lower.endswith((".txt", ".csv")):
        return ExtractedText(data.decode("utf-8", errors="replace"), mime=sniffed)
    return ExtractedText(None, mime=sniffed)


def extract_text(
    filename: str, mime: str, data: bytes, *, ocr: bool | OcrBackend | None = False
) -> ExtractedText:
    """`extract` with the old boolean `ocr` flag, which selects the Tesseract backend."""
    backend: OcrBackend | None
    backend = TesseractOcr() if ocr is True else (None if ocr is False else ocr)
    return extract(filename, mime, data, ocr=backend)
