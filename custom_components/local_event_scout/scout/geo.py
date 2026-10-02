"""Distance maths and OpenStreetMap (Nominatim) geocoding."""

from __future__ import annotations

import asyncio
from math import asin, cos, radians, sin, sqrt
import time
from typing import Any

import aiohttp

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "HA-EventSearch/0.3 (Home Assistant custom integration)"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


class Geocoder:
    """Nominatim client with a shared cache and the required 1 request/second pacing.

    ``cache`` maps query strings to ``[lat, lon]`` or ``None`` (known miss) and is
    owned by the caller so it can be persisted between runs.
    """

    def __init__(self, session: aiohttp.ClientSession, cache: dict[str, Any] | None = None) -> None:
        self._session = session
        self.cache: dict[str, Any] = cache if cache is not None else {}
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def lookup(self, query: str, country_hint: str = "") -> tuple[float, float] | None:
        key = " ".join(query.lower().split())
        if not key:
            return None
        if key in self.cache:
            hit = self.cache[key]
            return (hit[0], hit[1]) if hit else None
        async with self._lock:
            wait = 1.1 - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            params = {"q": query, "format": "jsonv2", "limit": "1"}
            try:
                async with self._session.get(
                    NOMINATIM_URL,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        return None  # transient; don't cache
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return None
        if not data:
            self.cache[key] = None
            return None
        try:
            point = (float(data[0]["lat"]), float(data[0]["lon"]))
        except (KeyError, IndexError, TypeError, ValueError):
            self.cache[key] = None
            return None
        self.cache[key] = [point[0], point[1]]
        return point
