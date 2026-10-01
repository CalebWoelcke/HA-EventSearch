"""Constants for Local Event Scout."""

DOMAIN = "local_event_scout"
PLATFORMS: list[str] = []
API_CONFIG = f"/api/{DOMAIN}/config"
API_RESULTS = f"/api/{DOMAIN}/results"
API_RUN = f"/api/{DOMAIN}/run"
PANEL_URL = DOMAIN
PANEL_COMPONENT = "local-event-scout-panel"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_ENGINE = "parallel"
DEFAULT_CONFIG = {
    "locations": [],
    "interests": [],
    "dislikes": [],
    "schedule": "02:30",
    "model": DEFAULT_MODEL,
    "search_engine": DEFAULT_ENGINE,
    "max_results": 8,
}
