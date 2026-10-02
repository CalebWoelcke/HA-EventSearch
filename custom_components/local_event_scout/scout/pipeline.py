"""(Saved sources + web search) → normalise → filter → locate → verify → rank."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from difflib import SequenceMatcher
import logging
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from . import prompts
from .dates import in_window, parse_event_time
from .geo import Geocoder, haversine_km
from .linkcheck import check_urls, resolve_redirects
from .models import PRIORITY_BOOST, Bucket, Interest, Location, Profile, Source
from .openrouter import OpenRouterError
from .providers import Completion, LLMProvider
from .sources import fetch_pages, host_of, normalize_url
from .textutil import clean_str, event_key, extract_json, normalize_title

_LOGGER = logging.getLogger(__name__)

MAP_TOLERANCE = 1.15  # allow 15% (+5 km) over the bucket distance for geocoded venues
ESTIMATE_TOLERANCE = 1.5  # model estimates are rough; be more lenient
MAX_GEOCODES_PER_RUN = 45
RANK_CHUNK = 30
PAGES_PER_EXTRACT = 3
CHARS_PER_EXTRACT = 36_000
SOURCE_SCORE = 7  # events scoring at least this suggest their listing page as a source
MAX_SUGGESTIONS = 5
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)


@dataclass
class RunResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    searches: int = 0  # model calls that used web search
    web_searches: int = 0  # individual web searches those calls made (when reported)
    pages_read: int = 0
    cost: float = 0.0
    candidates: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    buckets_scanned: list[str] = field(default_factory=list)
    buckets_searched: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    source_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    suggested_sources: list[dict[str, Any]] = field(default_factory=list)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def record(self, kind: str, label: str, completion: Completion) -> None:
        self.cost += completion.cost
        self.web_searches += completion.usage.get("web_searches") or 0
        self.calls.append({"kind": kind, "label": label, **completion.usage})


class ScoutPipeline:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        llm: LLMProvider,
        *,
        tz: tzinfo,
        tz_name: str,
        geocode_cache: dict[str, Any] | None = None,
        check_links: bool = True,
    ) -> None:
        self._session = session
        self._llm = llm
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
        search_bucket_ids: list[str] | None = None,
        sources: list[Source] | None = None,
        liked: list[str] | None = None,
        disliked: list[str] | None = None,
        weather: str = "",
        progress: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Process ``bucket_ids``: read their saved sources, and web-search those in ``search_bucket_ids``."""
        result = RunResult()
        report = progress or (lambda _msg: None)
        today = now.date()
        search_ids = set(bucket_ids if search_bucket_ids is None else search_bucket_ids) & set(bucket_ids)

        await self.locate_locations(profile.locations)
        home = profile.locations[0] if profile.locations else Location("home", "home")

        active = [b for b in profile.buckets if b.id in bucket_ids and profile.interests_for(b.id)]
        result.buckets_scanned = [b.id for b in active]
        result.buckets_searched = [b.id for b in active if b.id in search_ids]
        bucket_sources = {
            b.id: [s for s in (sources or []) if s.bucket == b.id] for b in active
        }

        raw_batches: list[tuple[Location, Bucket, list[dict[str, Any]], str]] = []

        # 1. Saved sources: fetch pages (no AI cost), then one cheap model call per few pages.
        to_read = [s for b in active for s in bucket_sources[b.id]]
        if to_read:
            report(f"Reading {len(to_read)} saved sources…")
            pages = {p.url: p for p in await fetch_pages(self._session, [s.url for s in to_read])}
            for bucket in active:
                readable = []
                for source in bucket_sources[bucket.id]:
                    page = pages[source.url]
                    result.source_stats[source.url] = {"found": 0, "error": page.error}
                    if page.text and page.error is None:
                        readable.append(page)
                result.pages_read += len(readable)
                for batch in _batches(readable):
                    events = await self._extract(home, bucket, profile, today, batch, result)
                    raw_batches.append((home, bucket, events, "source"))

        # 2. Web search, two at a time.
        jobs = [(loc, b) for b in active if b.id in search_ids for loc in profile.locations]
        if jobs:
            semaphore = asyncio.Semaphore(2)
            done = 0

            async def _search(loc: Location, bucket: Bucket):
                nonlocal done
                async with semaphore:
                    try:
                        events = await self._search(loc, bucket, profile, today, result)
                    except OpenRouterError as err:
                        result.errors.append(f"{bucket.label} search near {loc.city}: {err}")
                        events = []
                    done += 1
                    report(f"Searched {done} of {len(jobs)}")
                    return loc, bucket, events, "search"

            report(f"Searching the web ({len(jobs)} searches)…")
            raw_batches.extend(await asyncio.gather(*(_search(*job) for job in jobs)))

        # 3. Normalise, window-filter and de-duplicate.
        candidates: dict[str, dict[str, Any]] = {}
        for loc, bucket, raw_events, origin in raw_batches:
            for raw in raw_events:
                result.candidates += 1
                event = self._normalise(raw, loc, bucket, profile, now, result, origin)
                if event is None:
                    continue
                if event["source_url"] in result.source_stats:
                    result.source_stats[event["source_url"]]["found"] += 1
                existing = find_duplicate(candidates, event)
                if existing:
                    merge_event(existing, event)
                    result.drop("duplicate")
                else:
                    candidates[event["id"]] = event

        events = list(candidates.values())

        # 4. Swap search-engine redirect links for the real pages.
        redirects = [e["url"] for e in events if urlsplit(e["url"]).netloc in REDIRECT_HOSTS]
        if redirects:
            resolved = await resolve_redirects(self._session, redirects)
            for event in events:
                event["url"] = resolved.get(event["url"], event["url"])

        # 5. Distance check against the bucket of the matched interest.
        report("Checking venue locations…")
        events = await self._apply_distance(events, profile, result)

        # 6. Drop events whose links are clearly dead.
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

        # 7. Rank against the profile and past feedback, then apply interest priority.
        if events:
            report("Ranking events…")
            await self._rank(events, profile, today, liked or [], disliked or [], weather, result)
            for event in events:
                if event["score"] is not None:
                    event["ai_score"] = event["score"]
                    event["score"] = max(0, min(10, event["score"] + PRIORITY_BOOST.get(event["priority"], 0)))

        result.events = events
        result.suggested_sources = suggest_sources(events, sources or [])
        report("Done")
        return result

    # ------------------------------------------------------------------ search

    async def _search(
        self, loc: Location, bucket: Bucket, profile: Profile, today: date, result: RunResult
    ) -> list[dict[str, Any]]:
        prompt = prompts.search_prompt(
            today=today,
            tz_name=self._tz_name,
            location_text=loc.text,
            bucket=bucket,
            interests=profile.interests_for(bucket.id),
            dislikes=profile.dislikes,
            max_results=profile.max_results,
        )
        completion = await self._llm.complete(prompts.SEARCH_SYSTEM, prompt, web_search=True, reasoning="low")
        result.searches += 1
        result.record("search", f"{bucket.label} near {loc.city}", completion)
        return _events_from(completion.content)

    async def _extract(
        self, home: Location, bucket: Bucket, profile: Profile, today: date, pages, result: RunResult
    ) -> list[dict[str, Any]]:
        prompt = prompts.extract_prompt(
            today=today,
            tz_name=self._tz_name,
            location_text=home.text,
            bucket=bucket,
            interests=profile.interests_for(bucket.id),
            dislikes=profile.dislikes,
            pages=[(p.url, p.text) for p in pages],
            max_results=max(profile.max_results, 5 * len(pages)),
        )
        label = f"{bucket.label}: {', '.join(host_of(p.url) for p in pages)}"
        try:
            completion = await self._llm.complete(prompts.EXTRACT_SYSTEM, prompt, web_search=False, reasoning="low")
        except OpenRouterError as err:
            result.errors.append(f"Reading saved sources failed: {err}")
            for page in pages:
                result.source_stats[page.url]["error"] = str(err)
            return []
        result.record("source", label, completion)
        events = _events_from(completion.content)
        known = {normalize_url(p.url): p.url for p in pages}
        for event in events:
            # Attribute each event to the page it came from (fall back to the only page).
            claimed = known.get(normalize_url(str(event.get("source_url") or "")))
            event["source_url"] = claimed or (pages[0].url if len(pages) == 1 else "")
        return events

    # --------------------------------------------------------------- normalise

    def _normalise(
        self,
        raw: dict[str, Any],
        loc: Location,
        found_bucket: Bucket,
        profile: Profile,
        now: datetime,
        result: RunResult,
        origin: str,
    ) -> dict[str, Any] | None:
        title = clean_str(raw.get("title"), 160)
        url = clean_str(raw.get("url"), 500)
        source_url = clean_str(raw.get("source_url"), 500)
        if not url.startswith(("http://", "https://")) and source_url:
            url = source_url
        if not title or not url.startswith(("http://", "https://")):
            result.drop("missing_title_or_url")
            return None
        event_time = parse_event_time(clean_str(raw.get("start"), 40), clean_str(raw.get("end"), 40), self._tz)
        if event_time is None:
            result.drop("no_date")
            return None
        interest = _match_interest(clean_str(raw.get("interest"), 120), profile.interests)
        bucket = profile.bucket(interest.bucket) if interest else found_bucket
        if not in_window(event_time, now, bucket.lookahead_days):
            result.drop("outside_dates")
            return None
        start, end = event_time.iso()
        try:
            est = float(raw.get("est_distance_km"))
        except (TypeError, ValueError):
            est = None
        listing = clean_str(raw.get("listing_url"), 500)
        outdoor = raw.get("outdoor")
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
            "priority": interest.priority if interest else "normal",
            "outdoor": outdoor.lower() == "true" if isinstance(outdoor, str) else bool(outdoor),
            "bucket": bucket.id,
            "location_id": loc.id,
            "found_via": origin,
            "source_url": source_url if origin == "source" else "",
            "listing_url": listing if listing.startswith(("http://", "https://")) else "",
            "est_distance_km": est,
            "distance_km": None,
            "distance_source": None,
            "link_ok": None,
            "score": None,
            "ai_score": None,
            "why": "",
            "weather_note": "",
        }

    # ---------------------------------------------------------------- distance

    async def locate_locations(self, locations: list[Location]) -> None:
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
        today: date,
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
            prompt = prompts.rank_prompt(
                today=today,
                interests=profile.interests,
                bucket_labels=labels,
                dislikes=profile.dislikes,
                liked=liked[-20:],
                disliked=disliked[-20:],
                weather=weather,
                candidates=candidates,
            )
            try:
                completion = await self._llm.complete(prompts.RANK_SYSTEM, prompt, web_search=False, reasoning="none")
                result.record("rank", f"Score {len(chunk)} events", completion)
                parsed = extract_json(completion.content)
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


def _events_from(content: str) -> list[dict[str, Any]]:
    try:
        parsed = extract_json(content)
    except ValueError as err:
        _LOGGER.debug("Unparseable model output: %s", (content or "")[:1000])
        raise OpenRouterError("the model's answer was not valid JSON") from err
    events = parsed.get("events", []) if isinstance(parsed, dict) else parsed
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _batches(pages) -> list[list]:
    batches: list[list] = []
    current: list = []
    size = 0
    for page in pages:
        if current and (len(current) >= PAGES_PER_EXTRACT or size + len(page.text) > CHARS_PER_EXTRACT):
            batches.append(current)
            current, size = [], 0
        current.append(page)
        size += len(page.text)
    if current:
        batches.append(current)
    return batches


def suggest_sources(events: list[dict[str, Any]], known: list[Source]) -> list[dict[str, Any]]:
    """Listing pages behind well-scored events that are not saved yet."""
    seen = {normalize_url(s.url) for s in known}
    suggestions = []
    for event in sorted(events, key=lambda e: -(e.get("score") or 0)):
        url = normalize_url(event.get("listing_url") or "")
        if not url or url in seen or (event.get("score") or 0) < SOURCE_SCORE:
            continue
        seen.add(url)
        suggestions.append(
            {"url": event["listing_url"], "bucket": event["bucket"], "name": event.get("venue") or host_of(url)}
        )
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    return suggestions


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


def find_duplicate(candidates: dict[str, dict[str, Any]], event: dict[str, Any]) -> dict[str, Any] | None:
    if event["id"] in candidates:
        return candidates[event["id"]]
    title = normalize_title(event["title"])
    day = event["start"][:10]
    for other in candidates.values():
        if other["start"][:10] == day and SequenceMatcher(None, title, normalize_title(other["title"])).ratio() >= 0.85:
            return other
    return None


def merge_event(existing: dict[str, Any], new: dict[str, Any]) -> None:
    """Fill gaps in an existing candidate from a duplicate sighting."""
    for key in ("venue", "address", "town", "summary", "category", "end", "listing_url", "source_url"):
        if not existing.get(key) and new.get(key):
            existing[key] = new[key]
    if existing["all_day"] and not new["all_day"]:
        existing["start"], existing["end"], existing["all_day"] = new["start"], new["end"], False
