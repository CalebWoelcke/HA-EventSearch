"""Date parsing and window checks for model-supplied event times."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
import re

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class EventTime:
    start: datetime | date
    end: datetime | date | None
    all_day: bool

    @property
    def start_date(self) -> date:
        return self.start.date() if isinstance(self.start, datetime) else self.start

    @property
    def last_date(self) -> date:
        end = self.end or self.start
        return end.date() if isinstance(end, datetime) else end

    def iso(self) -> tuple[str, str | None]:
        return self.start.isoformat(), self.end.isoformat() if self.end else None


def parse_moment(value: str, tz: tzinfo) -> datetime | date | None:
    """Parse an ISO-ish date or datetime; naive datetimes get the local zone."""
    text = (value or "").strip()
    if not text:
        return None
    if _DATE_ONLY.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    text = text.replace(" ", "T", 1).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to just the date if the time part is unusable.
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def parse_event_time(start: str, end: str | None, tz: tzinfo) -> EventTime | None:
    start_value = parse_moment(start, tz)
    if start_value is None:
        return None
    end_value = parse_moment(end, tz) if end else None
    all_day = not isinstance(start_value, datetime)
    if end_value is not None:
        # Keep start/end the same kind so calendars can use them directly.
        if all_day and isinstance(end_value, datetime):
            end_value = end_value.date()
        elif not all_day and not isinstance(end_value, datetime):
            end_value = datetime.combine(end_value, time(23, 59), tzinfo=tz)
        if _as_date(end_value) < _as_date(start_value):
            end_value = None
    return EventTime(start_value, end_value, all_day)


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def in_window(event_time: EventTime, now: datetime, lookahead_days: int) -> bool:
    """True if the event has not finished and starts within the look-ahead window."""
    today = now.date()
    if event_time.last_date < today:
        return False
    if isinstance(event_time.end or event_time.start, datetime):
        finish = event_time.end or (event_time.start + timedelta(hours=3))
        if finish < now:
            return False
    return event_time.start_date <= today + timedelta(days=lookahead_days)
