"""Fixture-backed extractor for tests and offline evals: reads <fixture>.expected.json as LLM output."""

from __future__ import annotations

import json
from pathlib import Path

from bidtriage.extraction.postprocess import postprocess
from bidtriage.extraction.protocol import ExtractionInput
from bidtriage.extraction.schema import ExtractedOpportunity, ExtractionMeta, LLMExtraction


class FakeExtractor:
    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir

    def extract(self, item: ExtractionInput) -> ExtractedOpportunity:
        key = item.external_ref or item.message_id
        path = self.fixtures_dir / f"{key}.expected.json"
        if not path.exists():
            raise FileNotFoundError(f"No fixture expectation for {key}: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        llm = LLMExtraction.model_validate(raw)
        return postprocess(
            llm, item.sent_at, ExtractionMeta(model="fake", prompt_version="fixture")
        )


class StaticExtractor:
    """Returns a pre-built LLMExtraction; handy in unit tests."""

    def __init__(self, llm: LLMExtraction) -> None:
        self.llm = llm

    def extract(self, item: ExtractionInput) -> ExtractedOpportunity:
        return postprocess(self.llm, item.sent_at)
