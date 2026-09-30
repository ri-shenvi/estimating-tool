"""Directory-of-.eml source for dev and tests (SPEC-01 F1 manual upload analogue)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bidtriage.ingestion.eml import ParsedMessage
from bidtriage.ingestion.msg import parse_upload
from bidtriage.ingestion.protocol import PollResult

PATTERNS = ("*.eml", "*.msg")


class FileSource:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        return self._scan(state, limit=limit, since=None)

    def backfill(self, state: dict[str, Any], *, since: datetime, limit: int = 50) -> PollResult:
        return self._scan(state, limit=limit, since=since)

    def _scan(self, state: dict[str, Any], *, limit: int, since: datetime | None) -> PollResult:
        seen: set[str] = set(state.get("seen", []))
        out: list[tuple[str, ParsedMessage]] = []
        errors: list[str] = []
        more = False
        paths = sorted(p for pat in PATTERNS for p in self.directory.glob(pat))
        for path in paths:
            if path.name in seen:
                continue
            if len(out) >= limit:
                more = True
                break
            try:
                parsed = parse_upload(path.name, path.read_bytes())
            except Exception as e:  # noqa: BLE001 - record and move on
                errors.append(f"{path.name}: {e}")
                seen.add(path.name)
                continue
            seen.add(path.name)
            if since is not None and (parsed.sent_at or datetime.min.replace(tzinfo=UTC)) < since:
                continue
            out.append((path.name, parsed))
        return PollResult(out, {"seen": sorted(seen)}, errors, more_available=more)
