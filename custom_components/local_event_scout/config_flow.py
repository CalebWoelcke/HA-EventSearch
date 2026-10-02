"""Config flow for Local Event Scout: choose OpenRouter or Google Gemini."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GEMINI_KEY,
    CONF_GEMINI_SEARCH_KEY,
    CONF_PROVIDER,
    DEFAULT_MODEL,
    DOMAIN,
    PROVIDER_GEMINI,
    PROVIDER_OPENROUTER,
)
from .scout.openrouter import OpenRouterAuthError, OpenRouterClient, OpenRouterError
from .scout.providers import validate_gemini_key


async def _validate_openrouter(hass, api_key: str) -> str | None:
    try:
        await OpenRouterClient(async_get_clientsession(hass), api_key).key_info()
    except OpenRouterAuthError:
        return "invalid_auth"
    except OpenRouterError:
        return "cannot_connect"
    return None


class LocalEventScoutConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_show_menu(step_id="user", menu_options=[PROVIDER_OPENROUTER, PROVIDER_GEMINI])

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reconfigure", menu_options=[PROVIDER_OPENROUTER, PROVIDER_GEMINI])

    def _finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            keep = {k: v for k, v in entry.data.items() if k == "model"}
            return self.async_update_reload_and_abort(entry, data=keep | data)
        return self.async_create_entry(title="Local Event Scout", data=data)

    async def async_step_openrouter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if not (error := await _validate_openrouter(self.hass, api_key)):
                data = {CONF_PROVIDER: PROVIDER_OPENROUTER, CONF_API_KEY: api_key}
                if user_input.get("model"):
                    data["model"] = user_input["model"].strip()
                return self._finish(data)
            errors["base"] = error
        fields: dict[Any, Any] = {vol.Required(CONF_API_KEY): str}
        if self.source != SOURCE_RECONFIGURE:
            fields[vol.Optional("model", default=DEFAULT_MODEL)] = str
        return self.async_show_form(step_id="openrouter", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_gemini(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            search_key = user_input[CONF_GEMINI_SEARCH_KEY].strip()
            free_key = (user_input.get(CONF_GEMINI_KEY) or "").strip()
            if error := await validate_gemini_key(session, search_key):
                errors[CONF_GEMINI_SEARCH_KEY] = error
            elif free_key and (error := await validate_gemini_key(session, free_key)):
                errors[CONF_GEMINI_KEY] = error
            else:
                return self._finish(
                    {CONF_PROVIDER: PROVIDER_GEMINI, CONF_GEMINI_SEARCH_KEY: search_key, CONF_GEMINI_KEY: free_key}
                )
        schema = vol.Schema(
            {vol.Required(CONF_GEMINI_SEARCH_KEY): str, vol.Optional(CONF_GEMINI_KEY, default=""): str}
        )
        return self.async_show_form(step_id="gemini", data_schema=schema, errors=errors)
