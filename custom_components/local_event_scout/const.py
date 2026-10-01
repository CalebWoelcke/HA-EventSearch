"""Constants for Local Event Scout."""

from homeassistant.const import Platform

DOMAIN = "local_event_scout"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR]
VERSION = "0.2.0"

API_STATE = f"/api/{DOMAIN}/config"
API_RUN = f"/api/{DOMAIN}/run"
API_FEEDBACK = f"/api/{DOMAIN}/feedback"

PANEL_URL = DOMAIN
PANEL_COMPONENT = "local-event-scout-panel"
PANEL_STATIC = f"/{DOMAIN}_panel"

SIGNAL_UPDATE = f"{DOMAIN}_update"

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
SEARCH_ENGINES = ["auto", "exa", "parallel", "native"]
FEEDBACK_VERDICTS = ["like", "dislike", "dismiss", "clear"]

# Skip scans when less than this much of the OpenRouter key limit is left (USD).
BUDGET_FLOOR = 0.02
