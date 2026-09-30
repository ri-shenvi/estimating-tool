"""Jinja2 rendering for HTML (Outlook-safe tables, inline styles) and plain text."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.digest.snapshot import DigestSnapshot

_env = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(["html", "j2"], default_for_string=True, default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def fmt_dt(dt: datetime | None, now: datetime | None = None, time_known: bool = True) -> str:
    if dt is None:
        return "Due date unknown"
    local = dt.astimezone(BUSINESS_TZ)
    day = local.strftime("%a %b %-d")
    if now is not None:
        delta = (local.date() - now.astimezone(BUSINESS_TZ).date()).days
        if delta == 0:
            day = "TODAY"
        elif delta == 1:
            day = "tomorrow"
    if time_known:
        return f"{day}, {local.strftime('%-I:%M %p')}"
    return day


def days_left(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    d = (dt.astimezone(BUSINESS_TZ).date() - now.astimezone(BUSINESS_TZ).date()).days
    if d < 0:
        return "passed"
    if d == 0:
        return "today"
    return f"{d} day{'s' if d != 1 else ''}"


def band_label(band: str) -> str:
    return {"bid": "BID", "consider": "CONSIDER", "likely_pass": "LIKELY PASS", "pass": "PASS"}.get(
        band, band.upper()
    )


def humanize(s: str | None) -> str:
    return (s or "").replace("_", " ")


def truncate(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


_env.filters.update(
    {
        "fmt_dt": fmt_dt,
        "days_left": days_left,
        "band_label": band_label,
        "humanize": humanize,
        "truncate90": truncate,
    }
)


def render_html(snap: DigestSnapshot) -> str:
    return _env.get_template("digest.html.j2").render(s=snap, now=snap.generated_at)


def render_text(snap: DigestSnapshot) -> str:
    t = _env.get_template("digest.txt.j2")
    return t.render(s=snap, now=snap.generated_at)


def subject_line(snap: DigestSnapshot) -> str:
    d = snap.generated_at.astimezone(BUSINESS_TZ).strftime("%a %b %-d")
    if snap.quiet:
        return f"Bids · {d} · quiet morning"
    c = snap.counts
    parts = [f"{c.get('new', 0)} new"]
    if c.get("new_bid"):
        parts[-1] += f" ({c['new_bid']} to bid)"
    parts.append(f"{c.get('due_this_week', 0)} due this week")
    if c.get("needs_decision"):
        parts.append(f"{c['needs_decision']} need decision")
    prefix = "⚠︎ " if snap.degraded else ""
    return f"{prefix}Bids · {d} · " + " · ".join(parts)
