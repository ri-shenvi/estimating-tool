"""Claude-backed extractor (ADR-004).

One structured-output call per message via the Anthropic Python SDK. Thinking is adaptive by
default on Claude Opus 5.5; effort is configurable. Server-side refusal fallbacks are enabled by
default (`fallbacks="default"` with the matching beta header) so a policy decline is retried on
Anthropic's recommended fallback model inside the same request.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, cast

import anthropic

from bidtriage.extraction.postprocess import postprocess
from bidtriage.extraction.prompts import PROMPT_VERSION, system_prompt
from bidtriage.extraction.protocol import ExtractionInput, ExtractionRefusedError
from bidtriage.extraction.schema import ExtractedOpportunity, ExtractionMeta, LLMExtraction

log = logging.getLogger("bidtriage.extraction")

ATTACHMENT_CHAR_CAP = 12_000
MAX_ATTACHMENTS = 4
BODY_CHAR_CAP = 20_000
_PRIORITY_WORDS = ("itb", "invitation", "scope", "bid form", "bid-form", "addend", "bulletin")


def _prioritize(attachments):
    usable = [a for a in attachments if a.text and not a.large_document]

    def rank(a):
        name = a.filename.lower()
        return 0 if any(w in name for w in _PRIORITY_WORDS) else 1

    return sorted(usable, key=rank)[:MAX_ATTACHMENTS]


def build_user_content(item: ExtractionInput) -> tuple[str, bool]:
    """Render the message as delimited data. Returns (text, attachments_truncated).

    Attachment text arrives with `[page N]` markers from the PDF reader, which is what lets the
    model cite `attachment:<name>:p<page>` in `source_location`.
    """
    attachments_truncated = False
    parts = [
        "<message>",
        f"<sent_at>{item.sent_at.isoformat()}</sent_at>",
        f"<from>{item.from_name} <{item.from_addr}></from>",
        f"<to>{', '.join(item.to)}</to>",
        f"<subject>{item.subject}</subject>",
    ]
    body = item.body
    if len(body) > BODY_CHAR_CAP:
        body = body[: BODY_CHAR_CAP // 2] + "\n[...truncated...]\n" + body[-BODY_CHAR_CAP // 2 :]
    parts.append(f"<body>\n{body}\n</body>")
    if item.links:
        parts.append("<links>\n" + "\n".join(item.links[:50]) + "\n</links>")
    for a in _prioritize(item.attachments):
        text = a.text
        if len(text) > ATTACHMENT_CHAR_CAP:
            text = (
                text[: ATTACHMENT_CHAR_CAP * 2 // 3]
                + "\n[...truncated...]\n"
                + text[-ATTACHMENT_CHAR_CAP // 3 :]
            )
            attachments_truncated = True
        parts.append(f'<attachment name="{a.filename}">\n{text}\n</attachment>')
    skipped = [a.filename for a in item.attachments if a.large_document]
    if skipped:
        parts.append(
            "<large_documents_not_included>"
            + ", ".join(skipped)
            + "</large_documents_not_included>"
        )
    parts.append("</message>")
    return "\n".join(parts), attachments_truncated


class ClaudeExtractor:
    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        client: anthropic.Anthropic | None = None,
        prompt_version: str = PROMPT_VERSION,
        use_fallbacks: bool = True,
    ) -> None:
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()
        self.prompt_version = prompt_version
        self.use_fallbacks = use_fallbacks

    def extract(self, item: ExtractionInput) -> ExtractedOpportunity:
        content, truncated = build_user_content(item)
        started = time.monotonic()
        kwargs: dict = {}
        if self.use_fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=[
                {
                    "type": "text",
                    "text": system_prompt(self.prompt_version),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            output_format=LLMExtraction,
            output_config=cast(Any, {"effort": self.effort}),
            **kwargs,
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            log.warning("extraction refused model=%s category=%s", self.model, category)
            raise ExtractionRefusedError(f"refused: {category}")
        llm = response.parsed_output
        if llm is None:
            raise RuntimeError("structured output missing parsed_output")
        if truncated and "attachment_truncated" not in [f.value for f in llm.flags]:
            from bidtriage.extraction.schema import Flag

            llm.flags.append(Flag.attachment_truncated)
        meta = ExtractionMeta(
            model=response.model,
            prompt_version=self.prompt_version,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return postprocess(llm, item.sent_at, meta)


def estimate_cost_usd(input_tokens: int, output_tokens: int, model: str) -> float:
    """Rough spend meter for the daily cap (SPEC-09). Prices per MTok."""
    prices = {
        "claude-opus-5-5": (4.0, 20.0),
        "claude-sonnet-5-5": (2.0, 10.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    pin, pout = prices.get(model, (4.0, 20.0))
    return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout


def now_utc() -> datetime:
    from datetime import UTC

    return datetime.now(tz=UTC)
