"""Local Event Scout: AI-curated local events for Home Assistant."""

from __future__ import annotations

from pathlib import Path

import voluptuous as vol

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import async_register_views
from .const import DOMAIN, PANEL_COMPONENT, PANEL_STATIC, PANEL_URL, PLATFORMS, VERSION
from .coordinator import EventScoutCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type EventScoutConfigEntry = ConfigEntry[EventScoutCoordinator]

SCAN_SCHEMA = vol.Schema({vol.Optional("force", default=True): cv.boolean})


def loaded_coordinators(hass: HomeAssistant) -> list[EventScoutCoordinator]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register things that exist once per Home Assistant instance."""
    async_register_views(hass)

    async def _scan_now(call: ServiceCall) -> None:
        coordinators = loaded_coordinators(hass)
        if not coordinators:
            raise ServiceValidationError("Local Event Scout is not set up.")
        for coordinator in coordinators:
            try:
                coordinator.async_request_scan(force=call.data["force"])
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

    hass.services.async_register(DOMAIN, "scan_now", _scan_now, schema=SCAN_SCHEMA)

    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_STATIC, str(Path(__file__).parent / "frontend"), cache_headers=False)]
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EventScoutConfigEntry) -> bool:
    coordinator = EventScoutCoordinator(hass, entry)
    await coordinator.async_initialize()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if PANEL_URL not in hass.data.get(frontend.DATA_PANELS, {}):
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name=PANEL_COMPONENT,
            frontend_url_path=PANEL_URL,
            sidebar_title="Event Scout",
            sidebar_icon="mdi:calendar-search",
            module_url=f"{PANEL_STATIC}/event-scout-panel.js?v={VERSION}",
            require_admin=True,
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EventScoutConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
        frontend.async_remove_panel(hass, PANEL_URL)
    return unloaded
