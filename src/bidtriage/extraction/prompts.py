"""Versioned prompt loader. Prompts live in /prompts as markdown so non-engineers can review them."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_VERSION = "v2"
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


@lru_cache
def system_prompt(version: str = PROMPT_VERSION) -> str:
    return (_PROMPTS_DIR / f"extract_{version}.md").read_text(encoding="utf-8")
