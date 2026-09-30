"""URL harvesting and host classification (SPEC-01 F6)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
HOST_CLASSES = {
    "buildingconnected.com": "buildingconnected",
    "procore.com": "procore",
    "isqft.com": "isqft",
    "constructconnect.com": "isqft",
    "planhub.com": "planhub",
    "smartbidnet.com": "smartbid",
    "smartbid.co": "smartbid",
    "panteratools.com": "pantera",
    "dropbox.com": "dropbox",
    "box.com": "box",
    "sharefile.com": "sharefile",
    "egnyte.com": "egnyte",
    "sharepoint.com": "onedrive",
    "onedrive.live.com": "onedrive",
    "1drv.ms": "onedrive",
    "drive.google.com": "google_drive",
    "docs.google.com": "google_drive",
    "pennbid.net": "public_portal",
    "bidnetdirect.com": "public_portal",
}
_WRAPPERS = (
    "safelinks.protection.outlook.com",
    "sendgrid.net",
    "mailchimp",
    "list-manage.com",
    "click.",
    "links.",
    "urldefense",
)


def unwrap(url: str) -> tuple[str, bool]:
    host = urlparse(url).netloc.lower()
    if any(w in host for w in _WRAPPERS):
        qs = parse_qs(urlparse(url).query)
        for key in ("url", "u", "target", "redirect", "dest"):
            if key in qs and qs[key]:
                candidate = unquote(qs[key][0])
                if candidate.startswith("http"):
                    return candidate, False
        return url, True
    return url, False


def classify_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for suffix, cls in HOST_CLASSES.items():
        if host == suffix or host.endswith("." + suffix):
            return cls
    return "other"


def harvest_links(*texts: str) -> list[dict[str, str | bool]]:
    seen: set[str] = set()
    out: list[dict[str, str | bool]] = []
    for text in texts:
        for m in _URL.finditer(text or ""):
            raw = m.group(0).rstrip(".,;:")
            url, wrapped = unwrap(raw)
            if url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "host_class": classify_host(url), "wrapped": wrapped})
    return out
