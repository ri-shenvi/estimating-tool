"""Directory-of-.eml source for dev and tests (SPEC-01 F1 manual upload analogue)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bidtriage.ingestion.eml import ParsedMessage, parse_eml
from bidtriage.ingestion.protocol import PollResult


class FileSource:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        seen: set[str] = set(state.get("seen", []))
        out: list[tuple[str, ParsedMessage]] = []
        errors: list[str] = []
        for path in sorted(self.directory.glob("*.eml")):
            if path.name in seen or len(out) >= limit:
                continue
            try:
                out.append((path.name, parse_eml(path.read_bytes())))
                seen.add(path.name)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{path.name}: {e}")
        return PollResult(out, {"seen": sorted(seen)}, errors)
