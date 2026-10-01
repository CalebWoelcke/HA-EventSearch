"""Search → normalise → filter → locate → verify → rank."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from difflib import SequenceMatcher
import logging
from typing import Any

import aiohttp

from . import prompts
from .dates import in_window, parse_event_time
from .geo import Geocoder, haversine_km
from .linkcheck import check_urls
from .models import Bucket, Interest, Location, Profile
from .openrouter import OpenRouterClient, OpenRouterError
from .textutil import clean_str, event_key, extract_json, normalize_title

_LOGGER = logging.getLogger(__name__)

MAP_TOLERANCE = 1.15  # allow 15% (+5 km) over the bucket distance for geocoded venues
ESTIMATE_TOLERANCE = 1.5  # model estimates are rough; be more lenient
MAX_GEOCODES_PER_RUN = 45
RANK_CHUNK = 30


@dataclass
class RunResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    searches: int = 0
    cost: float = 0.0
    candidates: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    buckets_scanned: list[str] = field(default_factory=list)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


class ScoutPipeline:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: OpenRouterClient,
        *,
        model: str,
        engine: str = "auto",
        tz: tzinfo,
        tz_name: str,
        geocode_cache: dict[str, Any] | None = None,
        check_links: bool = True,
    ) -> None:
        self._session = session
        self._client = client
        self._model = model
        self._engine = engine
        self._tz = tz
        self._tz_name = tz_name
        self.geocoder = Geocoder(session, geocode_cache)
        self._check_links = check_links

    async def run(
        self,
        profile: Profile,
        *,
        now: datetime,
        bucket_ids: list[str],
        liked: list[str] | None = None,
        disliked: list[str] | None = None,
        weather: str = "",
        progress: Callable[[str], None] | None = None,
    ) -> RunResult:
        result = RunResult()
        report = progress or (lambda _msg: None)
        today = now.date()

        await self._locate_locations(profile.locations)

        jobs: list[tuple[Location, Bucket, list[Interest]]] = []
        for bucket in profile.buckets:
            interests = profile.interests_for(bucket.id)
            if bucket.id in bucket_ids and interests:
                result.buckets_scanned.append(bucket.id)
                jobs.extend((loc, bucket, interests) for loc in profile.locations)

        # 1. Search (two at a time to keep runs short without hammering the API).
        semaphore = asyncio.Semaphore(2)
        done = 0

        async def _search(loc: Location, bucket: Bucket, interests: list[Interest]):
            nonlocal done
            async with semaphore:
                try:
                    return loc, bucket, await self._search(loc, bucket, interests, profile, today, result)
                except OpenRouterError as err:
                    result.errors.append(f"{bucket.label} search near {loc.city}: {err}")
                    return loc, bucket, []
                finally:
                    done += 1
                    report(f"Searched {done} of {len(jobs)}")

        report(f"Searching ({len(jobs)} searches)…")
        raw_batches = await asyncio.gather(*(_search(*job) for job in jobs))

        # 2. Normalise, window-filter and de-duplicate.
        candidates: dict[str, dict[str, Any]] = {}
        for loc, bucket, raw_events in raw_batches:
            for raw in raw_events:
                result.candidates += 1
                event = self._normalise(raw, loc, bucket, profile, now, result)
                if event is None:
                    continue
                existing = _find_duplicate(candidates, event)
                if existing:
                    _merge(existing, event)
                    result.drop("duplicate")
                else:
                    candidates[event["id"]] = event

        # 3. Distance check against the bucket of the matched interest.
        report("Checking venue locations…")
        events = await self._apply_distance(list(candidates.values()), profile, result)

        # 4. Drop events whose links are clearly dead.
        if self._check_links and events:
            report("Checking links…")
            status = await check_urls(self._session, [e["url"] for e in events])
            kept = []
            for event in events:
                event["link_ok"] = status.get(event["url"])
                if event["link_ok"] is False:
                    result.drop("dead_link")
                else:
                    kept.append(event)
            events = kept

        # 5. Rank against the profile and past feedback.
        if events:
            report("Ranking events…")
            await self._rank(events, profile, today, liked or [], disliked or [], weather, result)
        result.events = events
        report("Done")
        return result

    # ------------------------------------------------------------------ search

    async def _search(
        self,
        loc: Location,
        bucket: Bucket,
        interests: list[Interest],
        profile: Profile,
        today,
        result: RunResult,
    ) -> list[dict[str, Any]]:
        tool: dict[str, Any] = {
            "type": "openrouter:web_search",
            "parameters": {"max_results": 10, "max_total_results": 25},
        }
        if self._engine and self._engine != "auto":
            tool["parameters"]["engine"] = self._engine
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": prompts.SEARCH_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.search_prompt(
                        today=today,
                        tz_name=self._tz_name,
                        location_text=loc.text,
                        bucket=bucket,
                        interests=interests,
                        dislikes=profile.dislikes,
                        max_results=profile.max_results,
                    ),
                },
            ],
            "tools": [tool],
            "temperature": 0.2,
        }
        chat = await self._client.chat(payload)
        result.searches += 1
        result.cost += chat.cost
        try:
            parsed = extract_json(chat.content)
        except ValueError as err:
            _LOGGER.debug("Unparseable search output: %s", chat.content[:1000])
            raise OpenRouterError("the model's answer was not valid JSON") from err
        events = parsed.get("events", []) if isinstance(parsed, dict) else parsed
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

    # --------------------------------------------------------------- normalise

    def _normalise(
        self,
        raw: dict[str, Any],
        loc: Location,
        search_bucket: Bucket,
        profile: Profile,
        now: datetime,
        result: RunResult,
    ) -> dict[str, Any] | None:
        title = clean_str(raw.get("title"), 160)
        url = clean_str(raw.get("url"), 500)
        if not title or not url.startswith(("http://", "https://")):
            result.drop("missing_title_or_url")
            return None
        event_time = parse_event_time(clean_str(raw.get("start"), 40), clean_str(raw.get("end"), 40), self._tz)
        if event_time is None:
            result.drop("no_date")
            return None
        interest = _match_interest(clean_str(raw.get("interest"), 120), profile.interests)
        bucket = profile.bucket(interest.bucket) if interest else search_bucket
        if not in_window(event_time, now, bucket.lookahead_days):
            result.drop("outside_dates")
            return None
        start, end = event_time.iso()
        try:
            est = float(raw.get("est_distance_km"))
        except (TypeError, ValueError):
            est = None
        return {
            "id": event_key(title, start),
            "title": title,
            "start": start,
            "end": end,
            "all_day": event_time.all_day,
            "venue": clean_str(raw.get("venue"), 120),
            "address": clean_str(raw.get("address"), 200),
            "town": clean_str(raw.get("town"), 80),
            "url": url,
            "summary": clean_str(raw.get("summary"), 300),
            "category": clean_str(raw.get("category"), 60),
            "interest": interest.name if interest else clean_str(raw.get("interest"), 120),
            "outdoor": bool(raw.get("outdoor")) if not isinstance(raw.get("outdoor"), str) else raw["outdoor"].lower() == "true",
            "bucket": bucket.id,
            "location_id": loc.id,
            "est_distance_km": est,
            "distance_km": None,
            "distance_source": None,
            "link_ok": None,
            "score": None,
            "why": "",
            "weather_note": "",
        }

    # ---------------------------------------------------------------- distance

    async def _locate_locations(self, locations: list[Location]) -> None:
        for loc in locations:
            if loc.lat is None or loc.lon is None:
                point = await self.geocoder.lookup(loc.text)
                if point:
                    loc.lat, loc.lon = point

    async def _apply_distance(
        self, events: list[dict[str, Any]], profile: Profile, result: RunResult
    ) -> list[dict[str, Any]]:
        origins = [(l.lat, l.lon) for l in profile.locations if l.lat is not None and l.lon is not None]
        loc_by_id = {l.id: l for l in profile.locations}
        lookups = 0
        kept = []
        for event in events:
            bucket = profile.bucket(event["bucket"])
            point = None
            if origins and lookups < MAX_GEOCODES_PER_RUN:
                loc = loc_by_id.get(event["location_id"])
                suffix = ", ".join(p for p in ((loc.region, loc.country) if loc else ()) if p)
                town = event["town"]
                queries = []
                if event["address"]:
                    queries.append(", ".join(p for p in (event["address"], town if town not in event["address"] else "", suffix) if p))
                if event["venue"] and town:
                    queries.append(f"{event['venue']}, {town}, {suffix}".strip(", "))
                if town:
                    queries.append(f"{town}, {suffix}".strip(", "))
                for query in queries:
                    lookups += 0 if query.lower() in self.geocoder.cache else 1
                    point = await self.geocoder.lookup(query)
                    if point:
                        break
            if point and origins:
                distance = min(haversine_km(o[0], o[1], point[0], point[1]) for o in origins)
                event["distance_km"] = round(distance, 1)
                event["distance_source"] = "map"
                if distance > bucket.km * MAP_TOLERANCE + 5:
                    result.drop("too_far")
                    continue
            elif event["est_distance_km"] is not None:
                event["distance_km"] = round(event["est_distance_km"], 1)
                event["distance_source"] = "estimate"
                if event["est_distance_km"] > bucket.km * ESTIMATE_TOLERANCE + 10:
                    result.drop("too_far")
                    continue
            kept.append(event)
        return kept

    # -------------------------------------------------------------------- rank

    async def _rank(
        self,
        events: list[dict[str, Any]],
        profile: Profile,
        today,
        liked: list[str],
        disliked: list[str],
        weather: str,
        result: RunResult,
    ) -> None:
        labels = {b.id: b.label for b in profile.buckets}
        by_id = {e["id"]: e for e in events}
        for start in range(0, len(events), RANK_CHUNK):
            chunk = events[start : start + RANK_CHUNK]
            candidates = [
                {
                    "id": e["id"],
                    "title": e["title"],
                    "date": e["start"][:16],
                    "venue": e["venue"],
                    "town": e["town"],
                    "category": e["category"],
                    "matched_interest": e["interest"],
                    "outdoor": e["outdoor"],
                    "summary": e["summary"],
                }
                for e in chunk
            ]
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": prompts.RANK_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.rank_prompt(
                            today=today,
                            interests=profile.interests,
                            bucket_labels=labels,
                            dislikes=profile.dislikes,
                            liked=liked[-20:],
                            disliked=disliked[-20:],
                            weather=weather,
                            candidates=candidates,
                        ),
                    },
                ],
                "temperature": 0.1,
            }
            try:
                chat = await self._client.chat(payload)
                result.cost += chat.cost
                parsed = extract_json(chat.content)
            except (OpenRouterError, ValueError) as err:
                result.errors.append(f"Ranking failed: {err}")
                continue
            rankings = parsed.get("rankings", []) if isinstance(parsed, dict) else parsed
            for item in rankings if isinstance(rankings, list) else []:
                if not isinstance(item, dict):
                    continue
                event = by_id.get(str(item.get("id")))
                if not event:
                    continue
                try:
                    event["score"] = max(0, min(10, round(float(item.get("score")))))
                except (TypeError, ValueError):
                    continue
                event["why"] = clean_str(item.get("why"), 200)
                event["weather_note"] = clean_str(item.get("weather_note"), 120)


def _match_interest(text: str, interests: list[Interest]) -> Interest | None:
    if not text or not interests:
        return None
    lowered = text.lower()
    for interest in interests:
        if interest.name.lower() == lowered:
            return interest
    for interest in interests:
        name = interest.name.lower()
        if name in lowered or lowered in name:
            return interest
    best = max(interests, key=lambda i: SequenceMatcher(None, i.name.lower(), lowered).ratio())
    return best if SequenceMatcher(None, best.name.lower(), lowered).ratio() >= 0.6 else None


def _find_duplicate(candidates: dict[str, dict[str, Any]], event: dict[str, Any]) -> dict[str, Any] | None:
    if event["id"] in candidates:
        return candidates[event["id"]]
    title = normalize_title(event["title"])
    day = event["start"][:10]
    for other in candidates.values():
        if other["start"][:10] == day and SequenceMatcher(None, title, normalize_title(other["title"])).ratio() >= 0.85:
            return other
    return None


def _merge(existing: dict[str, Any], new: dict[str, Any]) -> None:
    """Fill gaps in an existing candidate from a duplicate sighting."""
    for key in ("venue", "address", "town", "summary", "category", "end"):
        if not existing.get(key) and new.get(key):
            existing[key] = new[key]
    if existing["all_day"] and not new["all_day"]:
        existing["start"], existing["end"], existing["all_day"] = new["start"], new["end"], False
