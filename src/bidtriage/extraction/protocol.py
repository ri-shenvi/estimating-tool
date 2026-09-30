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
