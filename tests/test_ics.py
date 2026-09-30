from datetime import date, datetime, timedelta

from bidtriage.calendar import CalendarEvent, build_ics
from bidtriage.calendar.ics import escape, fold
from bidtriage.core.clock import BUSINESS_TZ


def test_events_render():  # type: ignore[no-untyped-def]
    due = datetime(2026, 10, 16, 14, tzinfo=BUSINESS_TZ)
    ics = build_ics(
        [
            CalendarEvent(
                "o1-due@bidtriage",
                "BID DUE: Benedum Hall (PJ Dick)",
                due - timedelta(minutes=30),
                timedelta(minutes=30),
                alarms_before=[timedelta(days=1), timedelta(hours=2)],
                sequence=2,
            ),
            CalendarEvent(
                "o1-prebid@bidtriage",
                "PRE-BID (MANDATORY): Benedum Hall (PJ Dick)",
                datetime(2026, 10, 7, 10, tzinfo=BUSINESS_TZ),
                timedelta(minutes=90),
                location="Benedum lobby",
            ),
            CalendarEvent(
                "o2-due@bidtriage", "BID DUE: all day", date(2026, 10, 8), None, cancelled=True
            ),
        ],
        now=datetime(2026, 9, 30, 10, tzinfo=BUSINESS_TZ),
    ).decode()
    assert "DTSTART:20261016T173000Z" in ics and "DTEND:20261016T180000Z" in ics
    assert "SEQUENCE:2" in ics and "TRIGGER:-P1D" in ics and "TRIGGER:-PT2H" in ics
    assert "DTSTART;VALUE=DATE:20261008" in ics and "STATUS:CANCELLED" in ics
    assert "TZID:America/New_York" in ics and "LOCATION:Benedum lobby" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_escape_and_fold():  # type: ignore[no-untyped-def]
    assert escape("a, b; c\nd") == "a\\, b\\; c\\nd"
    folded = fold("X:" + "y" * 200)
    lines = folded.split("\r\n")
    assert (
        len(lines) > 1
        and all(len(line.encode()) <= 75 for line in lines)
        and all(line.startswith(" ") for line in lines[1:])
    )
