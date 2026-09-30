"""Mailbox and attachment ingestion (SPEC-01, ADR-003)."""

from bidtriage.ingestion.eml import ParsedAttachment, ParsedMessage, parse_eml
from bidtriage.ingestion.links import classify_host, harvest_links
from bidtriage.ingestion.protocol import MailSource, PollResult

__all__ = [
    "parse_eml",
    "ParsedMessage",
    "ParsedAttachment",
    "harvest_links",
    "classify_host",
    "MailSource",
    "PollResult",
]
