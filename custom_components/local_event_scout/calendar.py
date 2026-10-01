"""Calendar of picked events."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import EventScoutEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([PicksCalendar(entry.runtime_data, "picks_calendar")])


def _to_calendar_event(event: dict[str, Any]) -> CalendarEvent | None:
    try:
        if event.get("all_day"):
            start: date | datetime = date.fromisoformat(event["start"][:10])
            last = date.fromisoformat((event.get("end") or event["start"])[:10])
            end: date | datetime = last + timedelta(days=1)  # end date is exclusive
        else:
            start = datetime.fromisoformat(event["start"])
            end = datetime.fromisoformat(event["end"]) if event.get("end") else start + timedelta(hours=2)
            if end <= start:
                end = start + timedelta(hours=2)
    except (KeyError, ValueError):
        return None
    score = event.get("score")
    summary = f"{event['title']} ({score}/10)" if score is not None else event["title"]
    description = "\n\n".join(p for p in (event.get("why"), event.get("summary"), event.get("weather_note"), event.get("url")) if p)
    location = ", ".join(p for p in (event.get("venue"), event.get("address") or event.get("town")) if p)
    return CalendarEvent(start=start, end=end, summary=summary, description=description, location=location, uid=event["id"])


class PicksCalendar(EventScoutEntity, CalendarEntity):
    _attr_icon = "mdi:calendar-star"

    def _events(self) -> list[CalendarEvent]:
        events = [_to_calendar_event(e) for e in self.coordinator.picks()]
        return sorted((e for e in events if e), key=lambda e: e.start_datetime_local)

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        for event in self._events():
            if event.end_datetime_local > now:
                return event
        return None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        return [
            e for e in self._events() if e.end_datetime_local > start_date and e.start_datetime_local < end_date
        ]
