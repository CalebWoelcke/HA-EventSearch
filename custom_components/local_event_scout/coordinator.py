"""Data, scheduling, and OpenRouter calls for Local Event Scout."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time
import hashlib
import json
import logging
import re
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DEFAULT_CONFIG, DOMAIN

_LOGGER = logging.getLogger(__name__)
_STORAGE_VERSION = 1


class EventScoutCoordinator:
    """Own one Event Scout configuration and its persisted results."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._config_store = Store[dict[str, Any]](
            hass, _STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.config"
        )
        self._results_store = Store[dict[str, Any]](
            hass, _STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.results"
        )
        self.config: dict[str, Any] = {}
        self.results: dict[str, Any] = {"items": [], "seen_ids": [], "last_run": None}
        self._unsub_schedule: Callable[[], None] | None = None
        self._scan_lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Load persisted state and schedule the nightly scan."""
        stored_config = await self._config_store.async_load() or {}
        self.config = {
            **DEFAULT_CONFIG,
            "model": self.entry.data.get("model", DEFAULT_CONFIG["model"]),
            **stored_config,
        }
        self.config["locations"] = list(self.config.get("locations", []))
        self.config["interests"] = list(self.config.get("interests", []))
        self.config["dislikes"] = list(self.config.get("dislikes", []))
        self.results = await self._results_store.async_load() or self.results
        self.results.setdefault("items", [])
        self.results.setdefault("seen_ids", [])
        self.results.setdefault("last_run", None)
        self._schedule()

    async def async_shutdown(self) -> None:
        """Tear down scheduled jobs."""
        if self._unsub_schedule:
            self._unsub_schedule()
            self._unsub_schedule = None

    def _schedule(self) -> None:
        """Register the configured once-daily local-time schedule."""
        if self._unsub_schedule:
            self._unsub_schedule()
        try:
            hour, minute = (int(value) for value in self.config["schedule"].split(":", 1))
            scheduled_time = time(hour=hour, minute=minute)
        except (AttributeError, TypeError, ValueError):
            scheduled_time = time(hour=2, minute=30)
            self.config["schedule"] = "02:30"

        async def _run_at_night(_: datetime) -> None:
            await self.async_scan()

        self._unsub_schedule = async_track_time_change(
            self.hass,
            _run_at_night,
            hour=scheduled_time.hour,
            minute=scheduled_time.minute,
            second=0,
        )

    async def async_save_config(self, incoming: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist user-editable configuration."""
        locations = []
        for raw in incoming.get("locations", []):
            city = str(raw.get("city", "")).strip()
            if not city:
                continue
            radius = max(1, min(500, int(raw.get("radius_km", 25))))
            locations.append(
                {
                    "id": str(raw.get("id") or uuid4()),
                    "city": city,
                    "region": str(raw.get("region", "")).strip(),
                    "country": str(raw.get("country", "")).strip(),
                    "radius_km": radius,
                }
            )

        schedule = str(incoming.get("schedule", self.config["schedule"]))
        try:
            datetime.strptime(schedule, "%H:%M")
        except ValueError as err:
            raise ValueError("Schedule must use 24-hour HH:MM format.") from err

        self.config = {
            **self.config,
            "locations": locations,
            "interests": _clean_words(incoming.get("interests", [])),
            "dislikes": _clean_words(incoming.get("dislikes", [])),
            "schedule": schedule,
            "model": str(incoming.get("model", self.config["model"])).strip(),
            "search_engine": str(
                incoming.get("search_engine", self.config["search_engine"])
            ).strip(),
            "max_results": max(1, min(20, int(incoming.get("max_results", 8)))),
        }
        await self._config_store.async_save(self.config)
        self._schedule()
        return self.public_state()

    def public_state(self) -> dict[str, Any]:
        """Return state safe to expose to the panel."""
        return {
            "config": self.config,
            "results": self.results,
            "scanning": self._scan_lock.locked(),
        }

    async def async_scan(self) -> dict[str, Any]:
        """Search every configured location and merge new events."""
        if not self.config["locations"]:
            raise ValueError("Add at least one location before running a search.")
        if not self.config["interests"]:
            raise ValueError("Add at least one interest before running a search.")

        async with self._scan_lock:
            discovered: list[dict[str, Any]] = []
            for location in self.config["locations"]:
                response = await self._call_openrouter(location)
                discovered.extend(response)

            seen = set(self.results["seen_ids"])
            new_items = []
            for item in discovered:
                item_id = _event_id(item)
                if item_id in seen:
                    continue
                seen.add(item_id)
                item["id"] = item_id
                item["dismissed"] = False
                item["discovered_at"] = dt_util.utcnow().isoformat()
                new_items.append(item)

            # Retain a bounded history while remembering more IDs for deduplication.
            self.results["items"] = (new_items + self.results["items"])[:250]
            self.results["seen_ids"] = list(seen)[-2000:]
            self.results["last_run"] = dt_util.utcnow().isoformat()
            await self._results_store.async_save(self.results)
            return self.public_state()

    async def _call_openrouter(self, location: dict[str, Any]) -> list[dict[str, Any]]:
        """Ask OpenRouter to search and return a compact event list."""
        location_text = ", ".join(
            part for part in [location["city"], location["region"], location["country"]] if part
        )
        prompt = f"""Find upcoming in-person events within {location['radius_km']} km of {location_text}
that happen in the next seven days. Use web search. The person's interests are:
{', '.join(self.config['interests'])}.
Avoid or strongly deprioritize: {', '.join(self.config['dislikes']) or 'nothing specified'}.

Return only JSON with this shape:
{{"events":[{{"title":"", "start":"ISO date/time or date", "venue":"", "location":"",
"url":"", "summary":"one concise sentence", "why_interesting":"", "source":""}}]}}.
Only include events with a specific date and a usable source URL. Return at most {self.config['max_results']} events.
"""
        payload = {
            "model": self.config["model"],
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "parameters": {
                        "engine": self.config["search_engine"] or "parallel",
                        "max_results": self.config["max_results"],
                        "max_total_results": self.config["max_results"],
                    },
                }
            ],
            "tool_choice": "required",
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        session = async_get_clientsession(self.hass)
        headers = {
            "Authorization": f"Bearer {self.entry.data[CONF_API_KEY]}",
            "Content-Type": "application/json",
            "X-Title": "Home Assistant Local Event Scout",
        }
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=60,
        ) as response:
            raw = await response.text()
            if response.status >= 400:
                _LOGGER.warning("OpenRouter search failed: %s", raw[:500])
                raise ValueError(f"OpenRouter returned HTTP {response.status}.")

        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
            parsed = json.loads(_extract_json(content))
            events = parsed.get("events", [])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as err:
            _LOGGER.warning("OpenRouter returned an unreadable event response: %s", raw[:500])
            raise ValueError("The model returned an unreadable event list. Try again.") from err

        normalized = []
        for event in events:
            if not isinstance(event, dict) or not event.get("title") or not event.get("url"):
                continue
            normalized.append(
                {
                    "title": str(event.get("title", "")).strip(),
                    "start": str(event.get("start", "Date not supplied")).strip(),
                    "venue": str(event.get("venue", "")).strip(),
                    "location": str(event.get("location", location_text)).strip(),
                    "url": str(event.get("url", "")).strip(),
                    "summary": str(event.get("summary", "")).strip(),
                    "why_interesting": str(event.get("why_interesting", "")).strip(),
                    "source": str(event.get("source", "")).strip(),
                }
            )
        return normalized


def _clean_words(values: Any) -> list[str]:
    """Normalize a user-supplied list of tags."""
    if not isinstance(values, list):
        values = str(values).split(",")
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _event_id(event: dict[str, Any]) -> str:
    """Generate a stable ID for deduplication."""
    raw = "|".join(str(event.get(key, "")) for key in ("url", "title", "start"))
    return hashlib.sha256(raw.lower().encode()).hexdigest()[:24]


def _extract_json(value: Any) -> str:
    """Handle providers that wrap JSON in a fenced code block."""
    text = str(value).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s*```$", "", text).strip()
