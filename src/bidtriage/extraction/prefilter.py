"""Cheap pre-filter: skip the LLM for obvious non-bid mail (SPEC-02 F1, ADR-004)."""

from __future__ import annotations

import re

from bidtriage.extraction.protocol import ExtractionInput

_NOISE_SUBJECT = re.compile(
    r"(unsubscribe|newsletter|webinar|your (weekly|daily) digest|password reset|invoice #|statement of account|out of office|automatic reply)",
    re.I,
)
_BID_HINT = re.compile(
    r"(invitation to bid|invited to bid|bid (due|date|package|form|invite)|request for (proposal|budget|quote|qualifications)|\bitb\b|\brfp\b|\brfq\b|addend|pre-?bid|bid opening|scope of work|proposal due)",
    re.I,
)


def obviously_not_bid(item: ExtractionInput) -> bool:
    """True only when it is safe to skip extraction entirely."""
    text = f"{item.subject}\n{item.body[:2000]}"
    if _BID_HINT.search(text):
        return False
    if _NOISE_SUBJECT.search(item.subject):
        return True
    if not item.attachments and len(item.body.strip()) < 40 and not item.links:
        return True
    return False
