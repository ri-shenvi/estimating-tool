"""Mailbox and attachment ingestion (SPEC-01, ADR-003)."""

from bidtriage.ingestion.attachments import (
    ExtractedText,
    Member,
    extract,
    extract_text,
    sanitize_filename,
    sniff_mime,
)
from bidtriage.ingestion.eml import ParsedAttachment, ParsedMessage, parse_eml, parse_rfc822
from bidtriage.ingestion.health import SourceHealth, source_health
from bidtriage.ingestion.links import classify_host, harvest_links
from bidtriage.ingestion.msg import is_msg, parse_msg, parse_upload
from bidtriage.ingestion.protocol import (
    BackfillableSource,
    FetchedMessage,
    FetchOptions,
    FetchSession,
    MailSource,
    PollSummary,
    supports_backfill,
)

__all__ = [
    "BackfillableSource",
    "ExtractedText",
    "FetchOptions",
    "FetchSession",
    "FetchedMessage",
    "MailSource",
    "Member",
    "ParsedAttachment",
    "ParsedMessage",
    "PollSummary",
    "SourceHealth",
    "classify_host",
    "extract",
    "extract_text",
    "harvest_links",
    "is_msg",
    "parse_eml",
    "parse_msg",
    "parse_rfc822",
    "parse_upload",
    "sanitize_filename",
    "sniff_mime",
    "source_health",
    "supports_backfill",
]
