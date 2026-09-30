"""Classification and extraction (SPEC-02, ADR-004)."""

from bidtriage.extraction.postprocess import postprocess
from bidtriage.extraction.protocol import ExtractionInput, Extractor
from bidtriage.extraction.schema import ExtractedOpportunity, Kind, LLMExtraction

__all__ = [
    "ExtractedOpportunity",
    "Kind",
    "LLMExtraction",
    "postprocess",
    "ExtractionInput",
    "Extractor",
]
