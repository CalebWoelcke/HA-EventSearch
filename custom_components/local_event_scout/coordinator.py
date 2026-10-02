"""Home Assistant glue: storage, scheduling, weather, spend, feedback and saved sources.

All search logic lives in the ``scout`` package; this module only feeds it
settings from Home Assistant and stores what comes back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BUDGET_FLOOR,
    CONF_GEMINI_KEY,
    CONF_GEMINI_SEARCH_KEY,
    CONF_PROVIDER,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_SEARCH_MODEL,
    DEFAULT_MODEL,
    DOMAIN,
    GEMINI_FREE_SEARCHES,
    PROVIDER_GEMINI,
    PROVIDER_OPENROUTER,
    SEARCH_ENGINES,
    SIGNAL_UPDATE,
    VERSION,
)
from .scout.dates import in_window, parse_event_time
from .scout.models import DEFAULT_BUCKET_ID, DEFAULT_BUCKETS, PRIORITIES, Bucket, Interest, Location, Profile, Source, to_dict
from .scout.openrouter import OpenRouterClient, OpenRouterError
from .scout.pipeline import ScoutPipeline, find_duplicate
from .scout.providers import GeminiProvider, OpenRouterProvider
from .scout.sources import host_of, normalize_url

_LOGGER = logging.getLogger(__name__)

CONFIG_VERSION = 2
RESULTS_VERSION = 2
SPEND_REFRESH = timedelta(hours=6)
MAX_FEEDBACK = 100
MAX_RUN_HISTORY = 60
MAX_SOURCES = 40
MAX_AUTO_SOURCES = 30
MAX_SOURCES_PER_RUN = 12
HEALTHY_MISSES = 3  # a source with fewer consecutive empty checks counts as working
AUTO_PAUSE_MISSES = 4  # auto-added sources are paused after this many empty checks
MIN_HEALTHY_SOURCES = 2  # groups with this many working sources only web-search every discovery_days

DEFAULT_CONFIG: dict[str, Any] = {
    "locations": [],
    "interests": [],
    "dislikes": [],
    "buckets": [to_dict(b) for b in DEFAULT_BUCKETS],
    "schedule": "02:30",
    "model": DEFAULT_MODEL,
    "search_engine": "auto",
    "gemini_model": DEFAULT_GEMINI_MODEL,
    "gemini_search_model": DEFAULT_GEMINI_SEARCH_MODEL,
    "max_results": 8,
    "min_score": 5,
    "weather_entity": "",  # "" = first weather entity, "none" = off
    "check_links": True,
    "auto_sources": True,
    "discovery_days": 7,
}


def _empty_results() -> dict[str, Any]:
    return {
        "events": {},
        "feedback": [],
        "last_run": None,
        "run_history": [],
        "bucket_scanned": {},
        "bucket_searched": {},
        "interest_searched": {},
        "sources": [],
        "geocode_cache": {},
        "spend": None,
    }


class _ConfigStore(Store[dict[str, Any]]):
    async def _async_migrate_func(self, old_major, old_minor, old_data):
        if old_major < 2:
            old_data = dict(old_data)
            old_data["interests"] = [
                {"name": str(i), "bucket": DEFAULT_BUCKET_ID} if not isinstance(i, dict) else i
                for i in old_data.get("interests", [])
            ]
            old_data["locations"] = [
                {k: v for k, v in loc.items() if k != "radius_km"} for loc in old_data.get("locations", [])
            ]
        return old_data


class _ResultsStore(Store[dict[str, Any]]):
    async def _async_migrate_func(self, old_major, old_minor, old_data):
        # v1 results had no parsed dates or scores; start fresh.
        return _empty_results() if old_major < 2 else old_data


class EventScoutCoordinator:
    """Owns settings, results and scans for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._config_store = _ConfigStore(hass, CONFIG_VERSION, f"{DOMAIN}.{entry.entry_id}.config")
        self._results_store = _ResultsStore(hass, RESULTS_VERSION, f"{DOMAIN}.{entry.entry_id}.results")
        self.config: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.results: dict[str, Any] = _empty_results()
        self.progress: str = ""
        self._scan_task: asyncio.Task | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_schedule: Callable[[], None] | None = None

    @property
    def provider(self) -> str:
        return self.entry.data.get(CONF_PROVIDER, PROVIDER_OPENROUTER)

    # ------------------------------------------------------------- lifecycle

    async def async_initialize(self) -> None:
        stored = await self._config_store.async_load() or {}
        self.config = self._validate_config({**DEFAULT_CONFIG, "model": self.entry.data.get("model", DEFAULT_MODEL), **stored})
        self.results = {**_empty_results(), **(await self._results_store.async_load() or {})}
        for source in self.results["sources"]:
            if source.get("origin") != "manual":
                source["name"] = host_of(source["url"])  # older versions named auto sources after a venue
        self._prune(dt_util.now())
        self._schedule()
        if self.provider == PROVIDER_OPENROUTER:
            self._unsubs.append(async_track_time_interval(self.hass, self._refresh_spend_cb, SPEND_REFRESH))
            self.entry.async_create_background_task(self.hass, self.async_refresh_spend(), f"{DOMAIN} spend")

    async def async_shutdown(self) -> None:
        if self._unsub_schedule:
            self._unsub_schedule()
            self._unsub_schedule = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()

    def _schedule(self) -> None:
        if self._unsub_schedule:
            self._unsub_schedule()
        hour, minute = (int(v) for v in self.config["schedule"].split(":"))

        @callback
        def _nightly(_now: datetime) -> None:
            if self.config["locations"] and self.config["interests"]:
                self._start_scan(force=False)

        self._unsub_schedule = async_track_time_change(self.hass, _nightly, hour=hour, minute=minute, second=0)

    @callback
    def _refresh_spend_cb(self, _now: datetime) -> None:
        self.entry.async_create_background_task(self.hass, self.async_refresh_spend(), f"{DOMAIN} spend")

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self.entry.entry_id}")

    # --------------------------------------------------------------- settings

    def _validate_config(self, incoming: dict[str, Any]) -> dict[str, Any]:
        old_locations = {loc["id"]: loc for loc in self.config.get("locations", []) if isinstance(loc, dict) and "id" in loc}
        locations = []
        for raw in incoming.get("locations") or []:
            if not isinstance(raw, dict):
                continue
            city = str(raw.get("city", "")).strip()
            if not city:
                continue
            loc = {
                "id": str(raw.get("id") or uuid4().hex),
                "city": city[:80],
                "region": str(raw.get("region", "")).strip()[:80],
                "country": str(raw.get("country", "")).strip()[:80],
                "lat": raw.get("lat"),
                "lon": raw.get("lon"),
            }
            previous = old_locations.get(loc["id"])
            if previous and any(previous.get(k) != loc[k] for k in ("city", "region", "country")):
                loc["lat"] = loc["lon"] = None  # place changed; geocode again
            if not isinstance(loc["lat"], (int, float)) or not isinstance(loc["lon"], (int, float)):
                loc["lat"] = loc["lon"] = None
            locations.append(loc)

        bucket_ids = {b.id for b in DEFAULT_BUCKETS}
        interests, seen = [], set()
        for raw in incoming.get("interests") or []:
            name = str(raw.get("name", "") if isinstance(raw, dict) else raw).strip()[:120]
            bucket = raw.get("bucket") if isinstance(raw, dict) else DEFAULT_BUCKET_ID
            priority = raw.get("priority") if isinstance(raw, dict) else "normal"
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            interests.append(
                {
                    "name": name,
                    "bucket": bucket if bucket in bucket_ids else DEFAULT_BUCKET_ID,
                    "priority": priority if priority in PRIORITIES else "normal",
                }
            )

        raw_buckets = {b.get("id"): b for b in incoming.get("buckets") or [] if isinstance(b, dict)}
        buckets = [to_dict(Bucket.from_dict(raw_buckets.get(d.id, {}), d)) for d in DEFAULT_BUCKETS]

        schedule = str(incoming.get("schedule") or "02:30")[:5]
        try:
            datetime.strptime(schedule, "%H:%M")
        except ValueError as err:
            raise ValueError("Nightly scan time must use 24-hour HH:MM format.") from err

        def _int(key: str, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(incoming.get(key, DEFAULT_CONFIG[key]))))
            except (TypeError, ValueError):
                return DEFAULT_CONFIG[key]

        def _str(key: str) -> str:
            return str(incoming.get(key) or DEFAULT_CONFIG[key]).strip()[:100]

        engine = str(incoming.get("search_engine") or "auto").strip().lower()
        return {
            "locations": locations[:5],
            "interests": interests[:100],
            "dislikes": _clean_words(incoming.get("dislikes", [])),
            "buckets": buckets,
            "schedule": schedule,
            "model": _str("model"),
            "search_engine": engine if engine in SEARCH_ENGINES else "auto",
            "gemini_model": _str("gemini_model"),
            "gemini_search_model": _str("gemini_search_model"),
            "max_results": _int("max_results", 1, 20),
            "min_score": _int("min_score", 0, 10),
            "weather_entity": str(incoming.get("weather_entity") or "").strip(),
            "check_links": bool(incoming.get("check_links", True)),
            "auto_sources": bool(incoming.get("auto_sources", True)),
            "discovery_days": _int("discovery_days", 1, 60),
        }

    async def async_save_config(self, incoming: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise ValueError("Settings must be a JSON object.")
        self.config = self._validate_config(incoming)
        await self._config_store.async_save(self.config)
        self._schedule()
        self._notify()
        return self.public_state()

    def profile(self) -> Profile:
        cfg = self.config
        return Profile(
            locations=[Location(**loc) for loc in cfg["locations"]],
            interests=[Interest(**i) for i in cfg["interests"]],
            dislikes=list(cfg["dislikes"]),
            buckets=[Bucket(**b) for b in cfg["buckets"]],
            min_score=cfg["min_score"],
            max_results=cfg["max_results"],
        )

    # ------------------------------------------------------------------ state

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    def upcoming_events(self) -> list[dict[str, Any]]:
        now = dt_util.now()
        tz = dt_util.get_default_time_zone()
        events = []
        for event in self.results["events"].values():
            event_time = parse_event_time(event["start"], event.get("end"), tz)
            if event_time and in_window(event_time, now, 3650):
                events.append(event)
        return sorted(events, key=lambda e: e["start"])

    def picks(self) -> list[dict[str, Any]]:
        min_score = self.config["min_score"]
        picks = [
            e
            for e in self.upcoming_events()
            if e.get("status") not in ("dislike", "dismiss") and (e.get("score") is None or e["score"] >= min_score)
        ]
        return sorted(picks, key=lambda e: (-(e.get("score") or 0), e["start"]))

    @property
    def status(self) -> str:
        if self.scanning:
            return "scanning"
        last = self.results.get("last_run")
        if not last:
            return "never"
        return last.get("status", "ok")

    def public_state(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "provider": self.provider,
            "config": self.config,
            "events": self.upcoming_events(),
            "sources": self.results["sources"],
            "last_run": self.results.get("last_run"),
            "status": self.status,
            "scanning": self.scanning,
            "progress": self.progress,
            "spend": self.results.get("spend"),
            "month_cost": self.month_cost(),
            "month_web_searches": self.month_web_searches(),
            "free_searches": GEMINI_FREE_SEARCHES if self.provider == PROVIDER_GEMINI else None,
            "next_due": self._next_due(),
            "weather_entities": sorted(self.hass.states.async_entity_ids("weather")),
        }

    def _this_month(self) -> list[dict[str, Any]]:
        month = dt_util.now().strftime("%Y-%m")
        return [r for r in self.results["run_history"] if r.get("started", "").startswith(month)]

    def month_cost(self) -> float:
        return round(sum(r.get("cost", 0) for r in self._this_month()), 4)

    def month_web_searches(self) -> int:
        return sum(r.get("web_searches") or 0 for r in self._this_month())

    def _next_due(self) -> dict[str, str]:
        today = dt_util.now().date()
        used = {i["bucket"] for i in self.config["interests"]}
        due = {}
        for bucket in self.config["buckets"]:
            if bucket["id"] not in used:
                continue
            last = self.results["bucket_scanned"].get(bucket["id"])
            if not last:
                due[bucket["id"]] = today.isoformat()
                continue
            next_day = datetime.fromisoformat(last).date() + timedelta(days=bucket["refresh_days"])
            due[bucket["id"]] = max(next_day, today).isoformat()
        return due

    # --------------------------------------------------------------- feedback

    async def async_set_feedback(self, event_id: str, verdict: str) -> dict[str, Any]:
        event = self.results["events"].get(event_id)
        if event is None:
            raise KeyError(event_id)
        event["status"] = "new" if verdict == "clear" else verdict
        log = [f for f in self.results["feedback"] if f.get("id") != event_id]
        if verdict in ("like", "dislike"):
            log.append(
                {
                    "id": event_id,
                    "title": event["title"],
                    "label": event.get("category") or event.get("interest") or "",
                    "verdict": verdict,
                    "at": dt_util.utcnow().isoformat(),
                }
            )
        self.results["feedback"] = log[-MAX_FEEDBACK:]
        if verdict == "like" and self.config["auto_sources"] and event.get("listing_url"):
            self._add_source(event["listing_url"], event["bucket"], event.get("venue") or "", "liked")
        await self._results_store.async_save(self.results)
        self._notify()
        return self.public_state()

    def _feedback_examples(self, verdict: str) -> list[str]:
        return [
            f"{f['title']} ({f['label']})" if f.get("label") else f["title"]
            for f in self.results["feedback"]
            if f.get("verdict") == verdict
        ]

    # ---------------------------------------------------------------- sources

    def _find_source(self, url: str) -> dict[str, Any] | None:
        key = normalize_url(url)
        return next((s for s in self.results["sources"] if normalize_url(s["url"]) == key), None)

    def _add_source(self, url: str, bucket: str, name: str, origin: str) -> dict[str, Any] | None:
        url = url.strip()
        if not normalize_url(url) or self._find_source(url):
            return None
        sources = self.results["sources"]
        if len(sources) >= MAX_SOURCES:
            return None
        if origin != "manual" and sum(1 for s in sources if s["origin"] != "manual") >= MAX_AUTO_SOURCES:
            return None
        source = {
            "url": url,
            "name": (name or host_of(url))[:80],
            "bucket": bucket if bucket in {b.id for b in DEFAULT_BUCKETS} else DEFAULT_BUCKET_ID,
            "origin": origin,  # manual | auto | liked
            "enabled": True,
            "added": dt_util.utcnow().isoformat(),
            "last_checked": None,
            "last_found": 0,
            "found_total": 0,
            "misses": 0,
            "last_error": None,
            "paused_reason": None,
        }
        sources.append(source)
        return source

    async def async_source_action(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        url = str(body.get("url") or "").strip()
        if action == "add":
            if not normalize_url(url):
                raise ValueError("Enter a full web address starting with http:// or https://.")
            if self._find_source(url):
                raise ValueError("That page is already saved.")
            if not self._add_source(url, str(body.get("bucket") or DEFAULT_BUCKET_ID), str(body.get("name") or ""), "manual"):
                raise ValueError(f"You can save up to {MAX_SOURCES} sources.")
        else:
            source = self._find_source(url)
            if source is None:
                raise KeyError(url)
            if action == "remove":
                self.results["sources"].remove(source)
            elif action == "update":
                if "bucket" in body and body["bucket"] in {b.id for b in DEFAULT_BUCKETS}:
                    source["bucket"] = body["bucket"]
                if "enabled" in body:
                    source["enabled"] = bool(body["enabled"])
                    if source["enabled"]:
                        source["misses"] = 0
                        source["paused_reason"] = None
                if body.get("name"):
                    source["name"] = str(body["name"])[:80]
            else:
                raise ValueError("Unknown source action.")
        await self._results_store.async_save(self.results)
        self._notify()
        return self.public_state()

    def _sources_to_read(self, bucket_ids: list[str]) -> list[Source]:
        candidates = [s for s in self.results["sources"] if s["enabled"] and s["bucket"] in bucket_ids]
        candidates.sort(key=lambda s: s.get("last_checked") or "")  # least recently checked first
        return [Source(s["url"], s["bucket"], s["name"]) for s in candidates[:MAX_SOURCES_PER_RUN]]

    def _healthy_sources(self, bucket_id: str) -> int:
        return sum(
            1 for s in self.results["sources"] if s["enabled"] and s["bucket"] == bucket_id and s["misses"] < HEALTHY_MISSES
        )

    def _update_source_stats(self, stats: dict[str, dict[str, Any]], when: str) -> None:
        for url, stat in stats.items():
            source = self._find_source(url)
            if source is None:
                continue
            source["last_checked"] = when
            source["last_found"] = stat["found"]
            source["found_total"] += stat["found"]
            source["last_error"] = stat["error"]
            source["misses"] = 0 if stat["found"] else source["misses"] + 1
            if source["origin"] != "manual" and source["misses"] >= AUTO_PAUSE_MISSES:
                source["enabled"] = False
                source["paused_reason"] = stat["error"] or f"No matching events in the last {AUTO_PAUSE_MISSES} checks"

    # ------------------------------------------------------------------ spend

    async def async_refresh_spend(self) -> None:
        if self.provider != PROVIDER_OPENROUTER:
            return
        client = OpenRouterClient(async_get_clientsession(self.hass), self.entry.data[CONF_API_KEY])
        try:
            info = await client.key_info()
        except OpenRouterError as err:
            _LOGGER.debug("Could not read OpenRouter key usage: %s", err)
            return
        self.results["spend"] = {
            key: info.get(key)
            for key in ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining", "limit_reset")
        } | {"fetched_at": dt_util.utcnow().isoformat()}
        self._notify()

    def _llm(self, session):
        if self.provider == PROVIDER_GEMINI:
            return GeminiProvider(
                session,
                search_key=self.entry.data[CONF_GEMINI_SEARCH_KEY],
                process_key=self.entry.data.get(CONF_GEMINI_KEY) or None,
                search_model=self.config["gemini_search_model"],
                process_model=self.config["gemini_model"],
            )
        return OpenRouterProvider(
            OpenRouterClient(session, self.entry.data[CONF_API_KEY]), self.config["model"], self.config["search_engine"]
        )

    # ------------------------------------------------------------------- scan

    def async_request_scan(self, force: bool = True) -> dict[str, Any]:
        """Validate and start a scan in the background; returns immediately."""
        if not self.config["locations"]:
            raise ValueError("Add at least one location before searching.")
        if not self.config["interests"]:
            raise ValueError("Add at least one interest before searching.")
        self._start_scan(force=force)
        return self.public_state()

    @callback
    def _start_scan(self, force: bool) -> None:
        if self.scanning:
            return
        self._scan_task = self.entry.async_create_background_task(
            self.hass, self._async_scan(force), f"{DOMAIN} scan"
        )
        # Entities read ``scanning`` from the task, so refresh them once it has finished.
        self._scan_task.add_done_callback(lambda _task: self._notify())
        self._notify()

    def _due_buckets(self, force: bool) -> list[str]:
        """Buckets that have interests and are due (or all of them when forced)."""
        used = {i["bucket"] for i in self.config["interests"]}
        today = dt_util.now().date().isoformat()
        due = self._next_due()
        return [b["id"] for b in self.config["buckets"] if b["id"] in used and (force or due[b["id"]] <= today)]

    def _search_buckets(self, bucket_ids: list[str], force: bool, now: datetime) -> list[str]:
        """Buckets to web-search: all when forced; otherwise skip groups with enough working sources."""
        if force:
            return list(bucket_ids)
        chosen = []
        for bucket_id in bucket_ids:
            last = self.results["bucket_searched"].get(bucket_id)
            recent = last and now - datetime.fromisoformat(last) < timedelta(days=self.config["discovery_days"], hours=-2)
            if self._healthy_sources(bucket_id) < MIN_HEALTHY_SOURCES or not recent:
                chosen.append(bucket_id)
        return chosen

    async def _async_scan(self, force: bool) -> None:
        bucket_ids = self._due_buckets(force)
        if not bucket_ids:
            _LOGGER.debug("Nightly check: no distance group is due")
            return
        started = dt_util.now()
        run: dict[str, Any] = {"started": started.isoformat(), "forced": force, "provider": self.provider}
        self.progress = "Starting…"
        self._notify()
        try:
            search_ids = self._search_buckets(bucket_ids, force, started)
            notes = []
            if self.provider == PROVIDER_OPENROUTER:
                await self.async_refresh_spend()
                remaining = (self.results.get("spend") or {}).get("limit_remaining")
                if isinstance(remaining, (int, float)) and remaining < BUDGET_FLOOR:
                    run.update(status="budget", error=f"OpenRouter limit nearly used up (${remaining:.2f} left); scan skipped.")
                    return
            elif search_ids and self.month_web_searches() >= GEMINI_FREE_SEARCHES - 50:
                search_ids = []
                notes.append("Monthly free Google searches nearly used up; only saved sources were read.")

            weather = await self._weather_summary()
            session = async_get_clientsession(self.hass)
            pipeline = ScoutPipeline(
                session,
                self._llm(session),
                tz=dt_util.get_default_time_zone(),
                tz_name=self.hass.config.time_zone,
                geocode_cache=self.results["geocode_cache"],
                check_links=self.config["check_links"],
            )
            profile = self.profile()
            # If a location cannot be geocoded, fall back to Home Assistant's home.
            await pipeline.locate_locations(profile.locations)
            for loc in profile.locations:
                if loc.lat is None and loc is profile.locations[0]:
                    loc.lat, loc.lon = self.hass.config.latitude, self.hass.config.longitude

            def _progress(message: str) -> None:
                self.progress = message
                self._notify()

            result = await pipeline.run(
                profile,
                now=started,
                bucket_ids=bucket_ids,
                search_bucket_ids=search_ids,
                sources=self._sources_to_read(bucket_ids),
                liked=self._feedback_examples("like"),
                disliked=self._feedback_examples("dislike"),
                weather=weather,
                known_events=self._known_events(),
                interest_last_searched=self.results["interest_searched"],
                progress=_progress,
            )
            new_count = self._merge_events(result.events)
            self._remember_coordinates(profile.locations)
            stamp = started.isoformat()
            for bucket_id in result.buckets_scanned:
                self.results["bucket_scanned"][bucket_id] = stamp
            for bucket_id in result.buckets_searched:
                self.results["bucket_searched"][bucket_id] = stamp
            for name in result.interests_searched:
                self.results["interest_searched"][name] = stamp
            self._update_source_stats(result.source_stats, stamp)
            added = []
            if self.config["auto_sources"]:
                for suggestion in result.suggested_sources:
                    if self._add_source(suggestion["url"], suggestion["bucket"], suggestion["name"], "auto"):
                        added.append(suggestion["url"])
            if added:
                notes.append(f"Saved {len(added)} new source{'s' if len(added) > 1 else ''}.")
            run.update(
                status="error" if result.errors and not result.events else "ok",
                error="; ".join(result.errors[:3]),
                note=" ".join(notes),
                searches=result.searches,
                web_searches=result.web_searches,
                pages_read=result.pages_read,
                cost=round(result.cost, 5),
                candidates=result.candidates,
                kept=len(result.events),
                new=new_count,
                dropped=result.dropped,
                buckets=result.buckets_scanned,
                buckets_searched=result.buckets_searched,
                interests_searched=result.interests_searched,
                calls=result.calls,
                sources_added=added,
            )
        except asyncio.CancelledError:
            run.update(status="error", error="Scan was cancelled.")
            raise
        except OpenRouterError as err:
            run.update(status="error", error=str(err))
        except Exception as err:  # noqa: BLE001 - keep the integration alive and show the problem
            _LOGGER.exception("Event Scout scan failed")
            run.update(status="error", error=f"Unexpected error: {err}")
        finally:
            run["finished"] = dt_util.now().isoformat()
            run.setdefault("cost", 0)
            self.results["last_run"] = run
            history_entry = {k: v for k, v in run.items() if k != "calls"}
            self.results["run_history"] = (self.results["run_history"] + [history_entry])[-MAX_RUN_HISTORY:]
            self._prune(dt_util.now())
            self.progress = ""
            await self._results_store.async_save(self.results)
            self._notify()
            if run.get("searches") or run.get("pages_read"):
                self.entry.async_create_background_task(self.hass, self.async_refresh_spend(), f"{DOMAIN} spend")

    def _known_events(self) -> dict[str, list[str]]:
        """Upcoming events per group, so searches spend their effort on new ones."""
        known: dict[str, list[str]] = {}
        for event in self.upcoming_events():
            known.setdefault(event["bucket"], []).append(f"{event['title']} ({event['start'][:10]})")
        return known

    def _merge_events(self, events: list[dict[str, Any]]) -> int:
        """Add new events and refresh known ones. Nothing is removed here (see _prune)."""
        stored: dict[str, dict[str, Any]] = self.results["events"]
        now = dt_util.utcnow().isoformat()
        new = 0
        for event in events:
            existing = stored.get(event["id"]) or find_duplicate(stored, event)
            if existing:
                keep = {k: existing[k] for k in ("id", "status", "first_seen") if k in existing}
                if event.get("score") is None and existing.get("score") is not None:
                    keep.update(score=existing["score"], why=existing.get("why", ""))
                existing.clear()
                existing.update(event | keep | {"last_seen": now})
            else:
                stored[event["id"]] = event | {"status": "new", "first_seen": now, "last_seen": now}
                new += 1
        return new

    def _remember_coordinates(self, locations: list[Location]) -> None:
        changed = False
        for loc, saved in zip(locations, self.config["locations"]):
            if saved.get("lat") is None and loc.lat is not None:
                saved["lat"], saved["lon"] = loc.lat, loc.lon
                changed = True
        if changed:
            self.hass.async_create_task(self._config_store.async_save(self.config))

    def _prune(self, now: datetime) -> None:
        """Remove events a day after they have finished."""
        tz = dt_util.get_default_time_zone()
        keep = {}
        for event_id, event in self.results["events"].items():
            event_time = parse_event_time(event.get("start", ""), event.get("end"), tz)
            if event_time and event_time.last_date >= now.date() - timedelta(days=1):
                keep[event_id] = event
        self.results["events"] = keep
        if len(self.results["geocode_cache"]) > 2000:
            self.results["geocode_cache"] = dict(list(self.results["geocode_cache"].items())[-1000:])

    # ---------------------------------------------------------------- weather

    async def _weather_summary(self) -> str:
        choice = self.config["weather_entity"]
        if choice == "none":
            return ""
        entity_id = choice or next(iter(sorted(self.hass.states.async_entity_ids("weather"))), None)
        if not entity_id or not self.hass.services.has_service("weather", "get_forecasts"):
            return ""
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "daily"},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - weather is optional
            _LOGGER.debug("Weather forecast unavailable from %s: %s", entity_id, err)
            return ""
        forecast = (response or {}).get(entity_id, {}).get("forecast", [])
        unit = self.hass.config.units.temperature_unit
        lines = []
        for day in forecast[:10]:
            when = dt_util.parse_datetime(str(day.get("datetime", "")))
            if not when:
                continue
            parts = [str(day.get("condition", "unknown"))]
            if day.get("temperature") is not None:
                parts.append(f"high {day['temperature']}{unit}")
            if day.get("templow") is not None:
                parts.append(f"low {day['templow']}{unit}")
            if day.get("precipitation_probability") is not None:
                parts.append(f"{day['precipitation_probability']}% chance of precipitation")
            lines.append(f"{dt_util.as_local(when):%a %Y-%m-%d}: " + ", ".join(parts))
        return "\n".join(lines)


def _clean_words(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = str(values or "").replace("\n", ",").split(",")
    return list(dict.fromkeys(str(v).strip()[:120] for v in values if str(v).strip()))[:100]
