"""Constants for Local Event Scout."""

from homeassistant.const import Platform

DOMAIN = "local_event_scout"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR]
VERSION = "0.3.0"

API_STATE = f"/api/{DOMAIN}/config"
API_RUN = f"/api/{DOMAIN}/run"
API_FEEDBACK = f"/api/{DOMAIN}/feedback"
API_SOURCES = f"/api/{DOMAIN}/sources"

PANEL_URL = DOMAIN
PANEL_COMPONENT = "local-event-scout-panel"
PANEL_STATIC = f"/{DOMAIN}_panel"

SIGNAL_UPDATE = f"{DOMAIN}_update"

CONF_PROVIDER = "provider"
CONF_GEMINI_SEARCH_KEY = "gemini_search_key"
CONF_GEMINI_KEY = "gemini_key"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_GEMINI = "gemini"

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"  # free tier, for reading pages and scoring
DEFAULT_GEMINI_SEARCH_MODEL = "gemini-3.1-flash-lite"  # paid project, cheapest grounded searches
GEMINI_FREE_SEARCHES = 5000  # free Google Search requests per month on a billing-enabled project
SEARCH_ENGINES = ["auto", "exa", "parallel", "native"]
FEEDBACK_VERDICTS = ["like", "dislike", "dismiss", "clear"]

# Skip scans when less than this much of the OpenRouter key limit is left (USD).
BUDGET_FLOOR = 0.02
