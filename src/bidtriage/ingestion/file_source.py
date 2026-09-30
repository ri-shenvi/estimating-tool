"""Directory-of-mail source for dev and tests (SPEC-01 F1 manual upload analogue).

Deliberately has no `seed`: the point of pointing bidtriage at a directory is to ingest what is in
it, so it declares `can_backfill = False` and its whole contents arrive on the first poll.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bidtriage.ingestion.msg import parse_upload
from bidtriage.ingestion.protocol import FetchedMessage, FetchOptions, FetchSession

PATTERNS = ("*.eml", "*.msg")


class FileSource:
    can_backfill = False

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def fetch(self, state: dict[str, Any], options: FetchOptions) -> FileFetchSession:
        return FileFetchSession(self, state, options)


class FileFetchSession(FetchSession):
    def __init__(self, source: FileSource, state: dict[str, Any], options: FetchOptions) -> None:
        super().__init__(state, options)
        self.source = source
        self._seen: set[str] = set(state.get("seen", []))
        self._noted: list[str] = []

    def _messages(self) -> Iterator[FetchedMessage]:
        paths = sorted(p for pat in PATTERNS for p in self.source.directory.glob(pat))
        for path in paths:
            if path.name in self._seen:
                continue
            self._noted.append(path.name)
            if self.pass_over(path.name):
                continue
            try:
                parsed = parse_upload(path.name, path.read_bytes())
            except Exception as e:  # noqa: BLE001 - recorded; retried until it is given up on
                self.fail(path.name, str(e))
                continue
            yield FetchedMessage(provider_message_id=path.name, parsed=parsed)

    def new_state(self) -> dict[str, Any]:
        """Only remember files that were stored, so a failed parse is retried next poll."""
        settled = {n for n in self._noted if self.settled(n)}
        return {**self.state, "seen": sorted(self._seen | settled)}
