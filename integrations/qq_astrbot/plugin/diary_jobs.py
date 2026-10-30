"""Timezone-aware daily boundaries, including missed runs and DST."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def resolve_timezone(name):
    if name == "system":
        from tzlocal import get_localzone
        return get_localzone()
    return ZoneInfo(name)


def diary_window(day, zone, hour=23, minute=30):
    """23:30–23:30: the last half-hour belongs to the following entry."""
    return (datetime.combine(day - timedelta(days=1), time(hour, minute), zone),
            datetime.combine(day, time(hour, minute), zone))


def due_dates(first_seen, now, hour=23, minute=30):
    if now.tzinfo is None:
        raise ValueError("Diary clock must be timezone aware")
    first = datetime.fromtimestamp(first_seen, now.tzinfo)
    day = first.date()
    if first >= datetime.combine(day, time(hour, minute), now.tzinfo):
        day += timedelta(days=1)
    last = now.date()
    if now < datetime.combine(last, time(hour, minute), now.tzinfo):
        last -= timedelta(days=1)
    while day <= last:
        yield day
        day += timedelta(days=1)
