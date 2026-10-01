"""Config flow for Local Event Scout."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_MODEL, DOMAIN
from .scout.openrouter import OpenRouterAuthError, OpenRouterClient, OpenRouterError


async def _validate_key(hass, api_key: str) -> str | None:
    """Return an error key, or None if the key works."""
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
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            api_key = user_input[CONF_API_KEY].strip()
            if not (error := await _validate_key(self.hass, api_key)):
                return self.async_create_entry(
                    title="Local Event Scout",
                    data={CONF_API_KEY: api_key, "model": user_input.get("model", "").strip() or DEFAULT_MODEL},
                )
            errors["base"] = error
        schema = vol.Schema(
            {vol.Required(CONF_API_KEY): str, vol.Optional("model", default=DEFAULT_MODEL): str}
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Replace the OpenRouter API key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if not (error := await _validate_key(self.hass, api_key)):
                return self.async_update_reload_and_abort(entry, data_updates={CONF_API_KEY: api_key})
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}), errors=errors
        )
