"""Status, picks and spending sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import EventScoutEntity

STATUS_OPTIONS = ["never", "ok", "scanning", "error", "budget"]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            StatusSensor(coordinator, "status"),
            PicksSensor(coordinator, "picks"),
            MonthlySpendSensor(coordinator, "monthly_spend"),
            WebSearchesSensor(coordinator, "web_searches"),
            LastRunCostSensor(coordinator, "last_run_cost"),
        ]
    )


class StatusSensor(EventScoutEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_icon = "mdi:calendar-search"

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.coordinator.results.get("last_run") or {}
        return {
            "progress": self.coordinator.progress,
            "last_started": last.get("started"),
            "last_finished": last.get("finished"),
            "last_error": last.get("error") or None,
            "searches": last.get("searches"),
            "candidates": last.get("candidates"),
            "kept": last.get("kept"),
            "new": last.get("new"),
            "next_due": self.coordinator._next_due(),  # noqa: SLF001
        }


class PicksSensor(EventScoutEntity, SensorEntity):
    _attr_icon = "mdi:star-circle-outline"
    _attr_native_unit_of_measurement = "events"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.picks())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        top = self.coordinator.picks()[:5]
        return {
            "top": [
                {k: e.get(k) for k in ("title", "start", "venue", "town", "score", "url")} for e in top
            ]
        }


class MonthlySpendSensor(EventScoutEntity, SensorEntity):
    """OpenRouter: real usage for the whole key this UTC month. Gemini: Event Scout's estimate."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        if self.coordinator.provider == "openrouter":
            return (self.coordinator.results.get("spend") or {}).get("usage_monthly")
        return self.coordinator.month_cost()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        spend = self.coordinator.results.get("spend") or {}
        attrs: dict[str, Any] = {
            "provider": self.coordinator.provider,
            "event_scout_this_month": self.coordinator.month_cost(),
        }
        if self.coordinator.provider == "openrouter":
            attrs |= {
                "limit": spend.get("limit"),
                "limit_remaining": spend.get("limit_remaining"),
                "limit_reset": spend.get("limit_reset"),
                "usage_today": spend.get("usage_daily"),
                "fetched_at": spend.get("fetched_at"),
            }
        else:
            attrs["estimated"] = True
        return attrs


class WebSearchesSensor(EventScoutEntity, SensorEntity):
    _attr_icon = "mdi:web"
    _attr_native_unit_of_measurement = "searches"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int:
        return self.coordinator.month_web_searches()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        free = 5000 if self.coordinator.provider == "gemini" else None
        return {"free_per_month": free} if free else {}


class LastRunCostSensor(EventScoutEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_suggested_display_precision = 4
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        last = self.coordinator.results.get("last_run")
        return last.get("cost") if last else None
