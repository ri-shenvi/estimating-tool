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


_GEOHASH_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float | None, lon: float | None, precision: int = 6) -> str:
    """Standard geohash. Used as a prefix-matchable "same area" bucket in candidate SQL.

    Six characters is roughly a 1.2 x 0.6 km cell, the scale SPEC-03 F2 reasons about; a shorter
    prefix widens the net. Cell boundaries mean neighbours can differ, so this only ever adds
    candidates — the 1 km test itself is done on the coordinates in `evidence()`.
    """
    if lat is None or lon is None:
        return ""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out: list[str] = []
    bit, ch, even = 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon > mid:
                ch |= 1 << (4 - bit)
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat > mid:
                ch |= 1 << (4 - bit)
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_GEOHASH_B32[ch])
            bit, ch = 0, 0
    return "".join(out)


def name_tokens(name: str | None) -> list[str]:
    """Normalized tokens long enough to be worth a LIKE, longest first.

    Candidate generation uses the longest one as a cheap SQL pre-filter where trigram search is
    unavailable (SQLite); `evidence()` still decides.
    """
    return sorted({t for t in normalize_name(name).split() if len(t) >= 4}, key=len, reverse=True)
