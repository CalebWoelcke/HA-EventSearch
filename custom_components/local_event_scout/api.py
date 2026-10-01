"""Authenticated HTTP endpoints used by the sidebar panel."""

from __future__ import annotations

from http import HTTPStatus
import json

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import API_FEEDBACK, API_RUN, API_STATE, DOMAIN, FEEDBACK_VERDICTS


def _coordinator(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    return None


class _ScoutView(HomeAssistantView):
    requires_auth = True

    def _get(self, request: web.Request):
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None:
            raise web.HTTPServiceUnavailable(text="Local Event Scout is not set up or still starting.")
        return coordinator

    @staticmethod
    def _require_admin(request: web.Request) -> None:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise web.HTTPForbidden(text="Only administrators can change Event Scout.")

    @staticmethod
    async def _body(request: web.Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as err:
            raise web.HTTPBadRequest(text="Request body must be JSON.") from err
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Request body must be a JSON object.")
        return body


class EventScoutStateView(_ScoutView):
    """GET current state; POST updated settings."""

    url = API_STATE
    name = f"api:{DOMAIN}:config"

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self._get(request).public_state())

    async def post(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        coordinator = self._get(request)
        try:
            state = await coordinator.async_save_config(await self._body(request))
        except ValueError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(state)


class EventScoutRunView(_ScoutView):
    """Start a scan in the background and return immediately."""

    url = API_RUN
    name = f"api:{DOMAIN}:run"

    async def post(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        coordinator = self._get(request)
        body = await self._body(request) if request.can_read_body else {}
        try:
            state = coordinator.async_request_scan(force=bool(body.get("force", True)))
        except ValueError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(state)


class EventScoutFeedbackView(_ScoutView):
    """Record like / dislike / dismiss for an event."""

    url = API_FEEDBACK
    name = f"api:{DOMAIN}:feedback"

    async def post(self, request: web.Request) -> web.Response:
        coordinator = self._get(request)
        body = await self._body(request)
        verdict = body.get("verdict")
        if verdict not in FEEDBACK_VERDICTS:
            return self.json_message(f"verdict must be one of {FEEDBACK_VERDICTS}", HTTPStatus.BAD_REQUEST)
        try:
            state = await coordinator.async_set_feedback(str(body.get("id")), verdict)
        except KeyError:
            return self.json_message("Event not found (it may have expired).", HTTPStatus.NOT_FOUND)
        return self.json(state)


def async_register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(EventScoutStateView)
    hass.http.register_view(EventScoutRunView)
    hass.http.register_view(EventScoutFeedbackView)
