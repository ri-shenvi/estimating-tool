"""Geocoding of the extracted location (SPEC-02 F3).

Part of extraction's post-processing, but the only part that talks to the network, so it is kept
apart from `postprocess` and injected. A failure is never fatal: `location.geo` stays null and
SPEC-04 scores the distance factor as unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from bidtriage.core.crypto import sha256_hex
from bidtriage.core.models import GeocodeCache
from bidtriage.extraction.schema import GeoLocation, GeoPrecision, Location

log = logging.getLogger("bidtriage.geocode")

# Nominatim's `addresstype` vocabulary, collapsed onto ours.
_PRECISION_BY_TYPE = {
    "building": GeoPrecision.exact,
    "house": GeoPrecision.exact,
    "house_number": GeoPrecision.exact,
    "amenity": GeoPrecision.exact,
    "place": GeoPrecision.exact,
    "road": GeoPrecision.street,
    "city": GeoPrecision.city,
    "town": GeoPrecision.city,
    "village": GeoPrecision.city,
    "borough": GeoPrecision.city,
    "suburb": GeoPrecision.city,
    "neighbourhood": GeoPrecision.city,
    "postcode": GeoPrecision.city,
    "hamlet": GeoPrecision.city,
    "county": GeoPrecision.region,
    "state": GeoPrecision.region,
}


@dataclass(frozen=True)
class GeoResult:
    lat: float
    lon: float
    # `none` means "the provider did not say"; the caller falls back to the precision it asked for.
    precision: GeoPrecision = GeoPrecision.none


class Geocoder(Protocol):
    def __call__(self, query: str) -> GeoResult | None: ...


def geocode_queries(loc: Location) -> list[tuple[str, GeoPrecision]]:
    """Queries to try, most precise first, each with the precision it would establish. Pure."""
    city_state = ", ".join(p for p in (loc.city, loc.state) if p)
    out: list[tuple[str, GeoPrecision]] = []
    if loc.street and (city_state or loc.postal_code):
        full = ", ".join(p for p in (loc.street, city_state) if p)
        out.append((" ".join(p for p in (full, loc.postal_code) if p), GeoPrecision.exact))
    if city_state:
        out.append((" ".join(p for p in (city_state, loc.postal_code) if p), GeoPrecision.city))
    elif loc.postal_code:
        out.append((loc.postal_code, GeoPrecision.city))
    if loc.raw and not out:
        # Nothing structured: "downtown Pittsburgh" still puts the job on the right side of town.
        out.append((loc.raw, GeoPrecision.city))
    seen: set[str] = set()
    unique: list[tuple[str, GeoPrecision]] = []
    for query, precision in out:
        if query.strip() and query not in seen:
            seen.add(query)
            unique.append((query, precision))
    return unique


def apply_geocode(loc: GeoLocation, geocoder: Geocoder | None) -> GeoLocation:
    """Fill lat/lon/geo_precision. Returns the location unchanged when nothing resolves."""
    if geocoder is None:
        return loc
    for query, asked in geocode_queries(loc):
        try:
            hit = geocoder(query)
        except Exception as e:  # noqa: BLE001 - a geocoder outage must not fail extraction
            log.warning("geocode failed for %r: %s", query, e)
            return loc
        if hit is not None:
            precision = hit.precision if hit.precision != GeoPrecision.none else asked
            return loc.model_copy(
                update={"lat": hit.lat, "lon": hit.lon, "geo_precision": precision}
            )
    return loc


class NominatimGeocoder:
    """Nominatim / OpenStreetMap-compatible forward geocoder (system-design §"External")."""

    def __init__(
        self,
        url: str,
        *,
        user_agent: str = "bidtriage",
        email: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.email = email
        self.timeout = timeout

    def __call__(self, query: str) -> GeoResult | None:
        import httpx

        params = {"q": query, "format": "jsonv2", "limit": "1"}
        if self.email:
            params["email"] = self.email
        response = httpx.get(
            self.url,
            params=params,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        row = rows[0]
        return GeoResult(
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            precision=_PRECISION_BY_TYPE.get(
                str(row.get("addresstype") or row.get("type") or ""), GeoPrecision.none
            ),
        )


class CachedGeocoder:
    """Memoizes hits in `geocode_cache` so the same job site is never geocoded twice.

    Misses are deliberately not cached: a miss and a provider outage look identical from here, and
    caching an outage would make the location permanently unknown.
    """

    def __init__(self, session: Session, inner: Geocoder) -> None:
        self.session = session
        self.inner = inner

    @staticmethod
    def key(query: str) -> str:
        return sha256_hex(" ".join(query.lower().split()).encode("utf-8"))

    def __call__(self, query: str) -> GeoResult | None:
        key = self.key(query)
        row = self.session.get(GeocodeCache, key)
        if row is not None:
            return GeoResult(row.lat, row.lon, GeoPrecision(row.precision))
        hit = self.inner(query)
        if hit is None:
            return None
        self.session.merge(
            GeocodeCache(
                query_key=key,
                query=query[:400],
                lat=hit.lat,
                lon=hit.lon,
                precision=hit.precision.value,
            )
        )
        self.session.flush()
        return hit
