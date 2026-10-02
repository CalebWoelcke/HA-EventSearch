import json
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from scout.models import DEFAULT_BUCKETS, Interest, Location, Profile
from scout.openrouter import OpenRouterError
from scout.pipeline import ScoutPipeline
from scout.providers import Completion

TZ = ZoneInfo("America/Edmonton")
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=TZ)
HOME = (53.5461, -113.4938)  # Edmonton


class FakeLLM:
    """Returns scripted search results, then ranks everything by title."""

    name = "fake"

    def __init__(self, search_events, fail_buckets=(), page_events=None):
        self.search_events = search_events
        self.fail_buckets = fail_buckets
        self.page_events = page_events or []
        self.calls = []

    async def complete(self, system, user, *, web_search, reasoning):
        self.calls.append({"system": system, "user": user, "web_search": web_search, "reasoning": reasoning})
        usage = {"cost": 0.01 if web_search else 0.002, "web_searches": 2 if web_search else 0, "input_tokens": 100}
        if web_search:
            for bucket_id in self.fail_buckets:
                if f"within about {dict((b.id, b.km) for b in DEFAULT_BUCKETS)[bucket_id]:g} km" in user:
                    raise OpenRouterError("boom")
            km = user.split("within about ")[1].split(" km")[0]
            events = self.search_events.get(km, [])
            return Completion("```json\n" + json.dumps({"events": events}) + "\n```", usage["cost"], usage)
        if "=== PAGE" in user:
            return Completion(json.dumps({"events": self.page_events}), usage["cost"], usage)
        candidates = json.loads(user.split("Candidates:\n")[1].split("\n\nReturn exactly")[0])
        rankings = [
            {"id": c["id"], "score": 2 if "boring" in c["title"].lower() else 7, "why": "fits", "weather_note": ""}
            for c in candidates
        ]
        return Completion(json.dumps({"rankings": rankings}), usage["cost"], usage)


def make_profile():
    return Profile(
        locations=[Location("home", "Edmonton", "Alberta", "Canada", *HOME)],
        interests=[Interest("trivia nights", "nearby", "high"), Interest("indie concerts", "local"), Interest("folk festivals", "daytrip", "low")],
        dislikes=["kids events"],
        buckets=list(DEFAULT_BUCKETS),
    )


def ev(title, start, town, interest, address="", est=None):
    return {
        "title": title,
        "start": start,
        "end": "",
        "venue": "Somewhere",
        "address": address,
        "town": town,
        "url": "https://example.com/" + title.replace(" ", "-"),
        "category": "x",
        "interest": interest,
        "outdoor": False,
        "est_distance_km": est,
        "summary": "s",
    }


async def run_pipeline(llm, geocache, bucket_ids=("nearby", "local", "daytrip", "travel"), **kwargs):
    async with aiohttp.ClientSession() as session:
        pipeline = ScoutPipeline(session, llm, tz=TZ, tz_name="America/Edmonton", geocode_cache=geocache, check_links=False)
        return await pipeline.run(make_profile(), now=NOW, bucket_ids=list(bucket_ids), **kwargs)


async def test_full_run_filters_and_ranks():
    geocache = {
        "somewhere, edmonton, alberta, canada": [53.55, -113.50],  # ~0 km
        "somewhere, st. albert, alberta, canada": [53.63, -113.63],  # ~13 km
        "somewhere, red deer, alberta, canada": [52.27, -113.81],  # ~143 km
        "somewhere, calgary, alberta, canada": [51.04, -114.07],  # ~280 km
    }
    client = FakeLLM(
        {
            "15": [
                ev("Pub Trivia", "2026-10-02T19:00", "Edmonton", "trivia nights"),
                ev("Trivia far away", "2026-10-03T19:00", "Red Deer", "trivia nights"),  # too far for Nearby
                ev("Old trivia", "2026-09-20", "Edmonton", "trivia nights"),  # past
            ],
            "50": [
                ev("Indie Band Live", "2026-10-05T20:00", "St. Albert", "indie concerts"),
                ev("Indie band live!", "2026-10-05", "St. Albert", "indie concerts"),  # duplicate
                ev("Boring indie thing", "2026-10-06", "Edmonton", "indie concerts"),
                ev("No link", "2026-10-06", "Edmonton", "indie concerts") | {"url": "n/a"},
            ],
            "150": [
                ev("Folk Fest", "2026-10-20", "Red Deer", "folk festivals"),
                ev("Folk Fest Calgary", "2026-10-21", "Calgary", "folk festivals"),  # 280 km > 150
            ],
        }
    )
    result = await run_pipeline(client, geocache)

    titles = {e["title"]: e for e in result.events}
    assert set(titles) == {"Pub Trivia", "Indie Band Live", "Boring indie thing", "Folk Fest"}
    assert result.dropped == {"too_far": 2, "outside_dates": 1, "duplicate": 1, "missing_title_or_url": 1}
    assert titles["Indie Band Live"]["all_day"] is False  # timed sighting wins over date-only
    assert titles["Boring indie thing"]["score"] == 2
    assert titles["Pub Trivia"]["score"] == 8 and titles["Pub Trivia"]["ai_score"] == 7  # high priority +1
    assert titles["Folk Fest"]["score"] == 6  # low priority -1
    assert titles["Folk Fest"]["bucket"] == "daytrip" and 140 < titles["Folk Fest"]["distance_km"] < 150
    assert result.searches == 3  # travel bucket has no interests
    assert abs(result.cost - 0.032) < 1e-9
    assert result.web_searches == 6 and [c["kind"] for c in result.calls].count("search") == 3
    assert {c["reasoning"] for c in client.calls if c["web_search"]} == {"low"}
    assert [c["reasoning"] for c in client.calls if not c["web_search"]] == ["none"]
    assert result.buckets_scanned == ["nearby", "local", "daytrip"]
    # every search prompt carries today's date
    assert all("Thursday, October 1, 2026" in c["user"] for c in client.calls)
    # high-priority interest is listed first and flagged
    assert "- trivia nights (top priority)" in client.calls[0]["user"]


async def test_one_failed_search_does_not_sink_the_run():
    geocache = {"somewhere, edmonton, alberta, canada": [53.55, -113.50]}
    client = FakeLLM({"50": [ev("Indie Band Live", "2026-10-05T20:00", "Edmonton", "indie concerts")]}, fail_buckets=("nearby",))
    result = await run_pipeline(client, geocache, ("nearby", "local"))
    assert [e["title"] for e in result.events] == ["Indie Band Live"]
    assert len(result.errors) == 1 and "Nearby" in result.errors[0]


async def test_estimate_used_when_geocoding_fails():
    geocache = {"nowhere, edmonton, alberta, canada": None, "edmonton, alberta, canada": None}
    near = ev("Close show", "2026-10-05", "Edmonton", "indie concerts", est=20) | {"venue": "Nowhere"}
    far = ev("Far show", "2026-10-05", "Edmonton", "indie concerts", est=300) | {"venue": "Nowhere"}
    result = await run_pipeline(FakeLLM({"50": [near, far]}), geocache, ("local",))
    assert [e["title"] for e in result.events] == ["Close show"]
    assert result.events[0]["distance_source"] == "estimate"


async def test_saved_sources_are_read_and_web_search_can_be_skipped(monkeypatch):
    import scout.pipeline as pipeline_mod
    from scout.models import Source
    from scout.sources import Page

    async def fake_fetch(session, urls, concurrency=4):
        return [
            Page(u, text="x" * 500) if "good" in u else Page(u, error="HTTP 403")
            for u in urls
        ]

    monkeypatch.setattr(pipeline_mod, "fetch_pages", fake_fetch)
    geocache = {"somewhere, edmonton, alberta, canada": [53.55, -113.50]}
    page_events = [ev("Starlite Indie Show", "2026-10-06T20:00", "Edmonton", "indie concerts") | {"source_url": "https://good.example/events/"}]
    llm = FakeLLM({"50": [ev("Searched show", "2026-10-07", "Edmonton", "indie concerts")]}, page_events=page_events)
    sources = [Source("https://good.example/events", "local", "Good venue"), Source("https://blocked.example/cal", "local")]

    result = await run_pipeline(llm, geocache, ("local",), search_bucket_ids=[], sources=sources)

    assert [e["title"] for e in result.events] == ["Starlite Indie Show"]
    assert result.events[0]["found_via"] == "source"
    assert result.events[0]["source_url"] == "https://good.example/events"
    assert result.searches == 0 and not any(c["web_search"] for c in llm.calls)
    assert result.source_stats == {
        "https://good.example/events": {"found": 1, "error": None},
        "https://blocked.example/cal": {"found": 0, "error": "HTTP 403"},
    }
    assert [c["kind"] for c in result.calls] == ["source", "rank"]


async def test_good_events_suggest_their_listing_pages():
    from scout.models import Source

    geocache = {"somewhere, edmonton, alberta, canada": [53.55, -113.50]}
    events = [
        ev("Great show", "2026-10-05", "Edmonton", "indie concerts") | {"listing_url": "https://venue.example/whats-on/"},
        ev("Boring show", "2026-10-06", "Edmonton", "indie concerts") | {"listing_url": "https://meh.example/cal"},
        ev("Known venue show", "2026-10-07", "Edmonton", "indie concerts") | {"listing_url": "https://known.example/events"},
    ]
    result = await run_pipeline(FakeLLM({"50": events}), geocache, ("local",), sources=[Source("https://known.example/events/", "local")])
    assert result.suggested_sources == [{"url": "https://venue.example/whats-on/", "bucket": "local", "name": "venue.example"}]


def test_pick_interests_rotates_and_keeps_high_priority():
    from scout.pipeline import pick_interests

    interests = [Interest("a"), Interest("b"), Interest("c", priority="high"), Interest("d"), Interest("e", priority="low")]
    last = {"a": "2026-10-01T02:30", "b": "2026-09-30T02:30"}
    picked = [i.name for i in pick_interests(interests, last, 3)]
    assert picked == ["c", "d", "e"]  # high first, then never-searched (normal before low)
    last |= {"d": "2026-10-02T02:30", "e": "2026-10-02T02:30"}
    assert [i.name for i in pick_interests(interests, last, 3)] == ["c", "b", "a"]  # then oldest first
    assert len(pick_interests(interests[:2], {}, 3)) == 2


async def test_known_events_are_listed_in_the_search_prompt():
    geocache = {"somewhere, edmonton, alberta, canada": [53.55, -113.50]}
    llm = FakeLLM({"50": []})
    await run_pipeline(llm, geocache, ("local",), known_events={"local": ["Indie Band Live (2026-10-05)"]})
    prompt = llm.calls[0]["user"]
    assert "already known" in prompt and "- Indie Band Live (2026-10-05)" in prompt


def test_find_duplicate_tolerates_one_day_shift_for_same_title():
    from scout.pipeline import find_duplicate

    stored = {"a": {"id": "a", "title": "Gas City Entertainment Expo 2026", "start": "2026-10-17"}}
    assert find_duplicate(stored, {"id": "b", "title": "Gas City Entertainment Expo 2026", "start": "2026-10-18T10:00"})
    assert not find_duplicate(stored, {"id": "c", "title": "Gas City Entertainment Expo 2026", "start": "2026-10-20"})
    assert not find_duplicate(stored, {"id": "d", "title": "Trivia Night", "start": "2026-10-18"})
