from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from bidtriage.extraction.schema import ExtractedOpportunity


@dataclass
class AttachmentText:
    filename: str
    text: str
    large_document: bool = False


@dataclass
class ExtractionInput:
    message_id: str
    subject: str
    from_addr: str
    from_name: str
    to: list[str]
    sent_at: datetime
    body: str
    attachments: list[AttachmentText] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    external_ref: str | None = None  # provider id / fixture stem; used by FakeExtractor


class Extractor(Protocol):
    def extract(self, item: ExtractionInput) -> ExtractedOpportunity: ...


class ExtractionError(RuntimeError):
    """An extraction that did not produce a record. Retried; then surfaced (SPEC-02 F3)."""

    category = "error"


class ExtractionRefusedError(ExtractionError):
    """The model declined. Retried like any failure, but never swept for retry as an outage."""

    category = "refusal"


def error_category(exc: BaseException) -> str:
    return getattr(exc, "category", "error") if isinstance(exc, ExtractionError) else "error"
