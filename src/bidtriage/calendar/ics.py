"""ICS feed rendering (SPEC-08). Hand-rolled RFC 5545 with folding and escaping; no external dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

VTIMEZONE_NY = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


@dataclass
class CalendarEvent:
    uid: str
    summary: str
    start: datetime | date
    duration: timedelta | None
    description: str = ""
    location: str | None = None
    sequence: int = 0
    last_modified: datetime | None = None
    cancelled: bool = False
    alarms_before: list[timedelta] = field(default_factory=list)


def escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold at 75 octets per RFC 5545 §3.1."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out: list[str] = []
    cur = b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74
        if len(cur) + len(b) > limit:
            out.append(cur.decode("utf-8"))
            cur = b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _event_lines(e: CalendarEvent, now: datetime) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{e.uid}",
        f"DTSTAMP:{_fmt_utc(now)}",
        f"SUMMARY:{escape(e.summary)}",
    ]
    if isinstance(e.start, datetime):
        lines.append(f"DTSTART:{_fmt_utc(e.start)}")
        if e.duration:
            lines.append(f"DTEND:{_fmt_utc(e.start + e.duration)}")
    else:
        lines.append(f"DTSTART;VALUE=DATE:{e.start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{(e.start + timedelta(days=1)).strftime('%Y%m%d')}")
    if e.description:
        lines.append(f"DESCRIPTION:{escape(e.description)}")
    if e.location:
        lines.append(f"LOCATION:{escape(e.location)}")
    lines.append(f"SEQUENCE:{e.sequence}")
    lines.append(f"LAST-MODIFIED:{_fmt_utc(e.last_modified or now)}")
    lines.append("STATUS:CANCELLED" if e.cancelled else "STATUS:CONFIRMED")
    for before in e.alarms_before:
        total = int(before.total_seconds())
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        trig = (
            "-P"
            + (f"{days}D" if days else "")
            + (
                "T" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
                if (hours or minutes)
                else ""
            )
        )
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{escape(e.summary)}",
            f"TRIGGER:{trig}",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_ics(
    events: list[CalendarEvent],
    *,
    calendar_name: str = "Ferry Electric Bids",
    now: datetime | None = None,
) -> bytes:
    now = now or datetime.now(tz=UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ferry Electric//bidtriage//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(calendar_name)}",
        "X-WR-TIMEZONE:America/New_York",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        *VTIMEZONE_NY.splitlines(),
    ]
    for e in events:
        lines += _event_lines(e, now)
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold(line) for line in lines) + "\r\n").encode("utf-8")
