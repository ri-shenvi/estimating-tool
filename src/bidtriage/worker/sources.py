"""Build a live mail-source implementation from a `sources` row (SPEC-01 F1).

Credentials live in `sources.config_enc`, encrypted with SECRET_KEY, so the database never holds a
readable client secret or app password.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bidtriage.core.crypto import decrypt, encrypt
from bidtriage.core.models import Source
from bidtriage.ingestion.file_source import FileSource
from bidtriage.ingestion.graph_source import GraphSource
from bidtriage.ingestion.imap_source import ImapSource
from bidtriage.ingestion.protocol import PollResult

KINDS = ("graph", "imap", "file", "manual")


class ManualSource:
    """Placeholder for the manual-upload pseudo-source: nothing to poll."""

    def poll(self, state: dict[str, Any], *, limit: int = 200) -> PollResult:
        return PollResult([], dict(state), [])


def encode_config(config: dict[str, Any], secret_key: str) -> str:
    return encrypt(json.dumps(config), secret_key)


def decode_config(src: Source, secret_key: str) -> dict[str, Any]:
    if not src.config_enc:
        return {}
    loaded = json.loads(decrypt(src.config_enc, secret_key))
    return dict(loaded) if isinstance(loaded, dict) else {}


def build_source(src: Source, secret_key: str) -> Any:
    cfg = decode_config(src, secret_key)
    folders = cfg.get("folders") or None
    if src.kind == "graph":
        return GraphSource(
            tenant_id=cfg["tenant_id"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            mailbox=cfg.get("mailbox") or src.mailbox,
            folders=folders,
        )
    if src.kind == "imap":
        return ImapSource(
            host=cfg["host"],
            port=int(cfg.get("port", 993)),
            user=cfg.get("user") or src.mailbox,
            password=cfg["password"],
            folders=folders,
            tls=bool(cfg.get("tls", True)),
        )
    if src.kind == "file":
        return FileSource(Path(cfg.get("directory") or src.name))
    if src.kind == "manual":
        return ManualSource()
    raise ValueError(f"unknown source kind {src.kind!r} (expected one of {KINDS})")
