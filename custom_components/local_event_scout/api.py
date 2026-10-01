"""Authenticated HTTP endpoints for the Local Event Scout panel."""

from __future__ import annotations

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import API_CONFIG, API_RESULTS, API_RUN, DOMAIN


def _coordinator(hass: HomeAssistant):
    """Return the single configured coordinator."""
    entries = hass.data.get(DOMAIN, {})
    for key, value in entries.items():
        if key not in {"views_registered", "panel_registered"}:
            return value
    raise web.HTTPNotFound(text="Local Event Scout is not configured.")


class EventScoutConfigView(HomeAssistantView):
    """Read and update Event Scout preferences."""

    url = API_CONFIG
    name = f"api:{DOMAIN}:config"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        return self.json(_coordinator(request.app["hass"]).public_state())

    async def post(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        try:
            state = await coordinator.async_save_config(await request.json())
        except (TypeError, ValueError) as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return self.json(state)


class EventScoutResultsView(HomeAssistantView):
    """Return the saved result history."""

    url = API_RESULTS
    name = f"api:{DOMAIN}:results"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        return self.json(_coordinator(request.app["hass"]).public_state())


class EventScoutRunView(HomeAssistantView):
    """Run a scan immediately."""

    url = API_RUN
    name = f"api:{DOMAIN}:run"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        try:
            state = await _coordinator(request.app["hass"]).async_scan()
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return self.json(state)


def async_register_views(hass: HomeAssistant) -> None:
    """Register views once per Home Assistant instance."""
    hass.http.register_view(EventScoutConfigView)
    hass.http.register_view(EventScoutResultsView)
    hass.http.register_view(EventScoutRunView)
