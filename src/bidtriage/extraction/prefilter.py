"""Cheap pre-filter: skip the LLM for obvious non-bid mail (SPEC-02 F1, ADR-004)."""

from __future__ import annotations

import re

from bidtriage.extraction.protocol import ExtractionInput

BODY_SCAN_CHARS = 2_000
# SPEC-02 F1: classification reads attachment filenames and the head of their text too. A platform
# digest subject over a real invitation letter is otherwise skipped, and a skipped bid is invisible.
ATTACHMENT_SCAN_CHARS = 3_000

_NOISE_SUBJECT = re.compile(
    r"(unsubscribe|newsletter|webinar|your (weekly|daily) digest|password reset|invoice #|statement of account|out of office|automatic reply)",
    re.I,
)
_BID_HINT = re.compile(
    r"(invitation to bid|invited to bid|bid (due|date|package|form|invite)|request for (proposal|budget|quote|qualifications)|\bitb\b|\brfp\b|\brfq\b|addend|pre-?bid|bid opening|scope of work|proposal due)",
    re.I,
)


def classification_text(item: ExtractionInput) -> str:
    """Everything F1 says the classification may look at, short of the full documents."""
    parts = [item.subject, item.from_name, item.from_addr, item.body[:BODY_SCAN_CHARS]]
    for a in item.attachments:
        parts.append(a.filename)
        if a.text:
            parts.append(a.text[:ATTACHMENT_SCAN_CHARS])
    return "\n".join(parts)


def obviously_not_bid(item: ExtractionInput) -> bool:
    """True only when it is safe to skip extraction entirely."""
    if _BID_HINT.search(classification_text(item)):
        return False
    if _NOISE_SUBJECT.search(item.subject):
        return True
    if not item.attachments and len(item.body.strip()) < 40 and not item.links:
        return True
    return False
