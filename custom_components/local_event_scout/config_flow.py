"""Config flow for Local Event Scout."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY

from .const import DEFAULT_MODEL, DOMAIN


class LocalEventScoutConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Local Event Scout."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Set up the OpenRouter credentials."""
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Local Event Scout",
                data={
                    CONF_API_KEY: user_input[CONF_API_KEY].strip(),
                    "model": user_input["model"].strip() or DEFAULT_MODEL,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Optional("model", default=DEFAULT_MODEL): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
