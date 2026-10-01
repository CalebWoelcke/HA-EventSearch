"""Home Assistant integration for Local Event Scout."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig

from .api import async_register_views
from .const import DOMAIN, PANEL_COMPONENT, PANEL_URL
from .coordinator import EventScoutCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Local Event Scout from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = EventScoutCoordinator(hass, entry)
    await coordinator.async_initialize()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    if not hass.data[DOMAIN].get("views_registered"):
        async_register_views(hass)
        hass.data[DOMAIN]["views_registered"] = True

    if not hass.data[DOMAIN].get("panel_registered"):
        frontend_path = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    url_path=f"/{DOMAIN}_panel",
                    path=str(frontend_path),
                    cache_headers=False,
                )
            ]
        )
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name=PANEL_COMPONENT,
            frontend_url_path=PANEL_URL,
            sidebar_title="Event Scout",
            sidebar_icon="mdi:calendar-search",
            module_url=f"/{DOMAIN}_panel/event-scout-panel.js?v=0.1.1",
            require_admin=False,
        )
        hass.data[DOMAIN]["panel_registered"] = True

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
    if coordinator:
        await coordinator.async_shutdown()
    return True
