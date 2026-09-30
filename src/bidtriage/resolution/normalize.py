from __future__ import annotations

import hashlib
import re
from datetime import datetime

_STOP = {
    "project",
    "new",
    "renovation",
    "renovations",
    "bid",
    "package",
    "electrical",
    "the",
    "and",
    "of",
    "for",
    "at",
    "a",
    "an",
    "bldg",
    "building",
    "phase",
    "rebid",
    "re-bid",
    "itb",
    "rfp",
    "invitation",
    "to",
    "tenant",
    "improvement",
    "improvements",
    "fit",
    "out",
    "fitout",
    "fit-out",
    "ti",
}
_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6"}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    tokens = []
    for t in s.split():
        t = _NUM_WORDS.get(t, t)
        if t in _STOP:
            continue
        tokens.append(t)
    return " ".join(tokens)


def normalize_domain(email_or_domain: str | None) -> str:
    if not email_or_domain:
        return ""
    d = email_or_domain.rsplit("@", 1)[-1].strip().lower()
    return d[4:] if d.startswith("www.") else d


def fingerprint(
    name: str | None, gc_domain: str | None, city: str | None, due: datetime | None
) -> str:
    week = due.strftime("%G-%V") if due else ""
    raw = "|".join(
        [normalize_name(name), normalize_domain(gc_domain), (city or "").strip().lower(), week]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
