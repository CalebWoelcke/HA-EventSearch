import json
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from scout.models import DEFAULT_BUCKETS, Interest, Location, Profile
from scout.openrouter import ChatResult, OpenRouterError
from scout.pipeline import ScoutPipeline

TZ = ZoneInfo("America/Edmonton")
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=TZ)
HOME = (53.5461, -113.4938)  # Edmonton


class FakeClient:
    """Returns scripted search results, then ranks everything by title."""

    def __init__(self, search_events, fail_buckets=()):
        self.search_events = search_events
        self.fail_buckets = fail_buckets
        self.calls = []

    async def chat(self, payload):
        self.calls.append(payload)
        prompt = payload["messages"][-1]["content"]
        if "tools" in payload:
            for bucket_id in self.fail_buckets:
                if f"within about {dict((b.id, b.km) for b in DEFAULT_BUCKETS)[bucket_id]:g} km" in prompt:
                    raise OpenRouterError("boom")
            km = prompt.split("within about ")[1].split(" km")[0]
            events = self.search_events.get(km, [])
            return ChatResult(content="```json\n" + json.dumps({"events": events}) + "\n```", cost=0.01, raw={})
        candidates = json.loads(prompt.split("Candidates:\n")[1].split("\n\nReturn exactly")[0])
        rankings = [
            {"id": c["id"], "score": 2 if "boring" in c["title"].lower() else 8, "why": "fits", "weather_note": ""}
            for c in candidates
        ]
        return ChatResult(content=json.dumps({"rankings": rankings}), cost=0.002, raw={})


def make_profile():
    return Profile(
        locations=[Location("home", "Edmonton", "Alberta", "Canada", *HOME)],
        interests=[Interest("trivia nights", "nearby"), Interest("indie concerts", "local"), Interest("folk festivals", "daytrip")],
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


async def run_pipeline(client, geocache, bucket_ids=("nearby", "local", "daytrip", "travel")):
    async with aiohttp.ClientSession() as session:
        pipeline = ScoutPipeline(
            session, client, model="m", tz=TZ, tz_name="America/Edmonton", geocode_cache=geocache, check_links=False
        )
        return await pipeline.run(make_profile(), now=NOW, bucket_ids=list(bucket_ids))


async def test_full_run_filters_and_ranks():
    geocache = {
        "somewhere, edmonton, alberta, canada": [53.55, -113.50],  # ~0 km
        "somewhere, st. albert, alberta, canada": [53.63, -113.63],  # ~13 km
        "somewhere, red deer, alberta, canada": [52.27, -113.81],  # ~143 km
        "somewhere, calgary, alberta, canada": [51.04, -114.07],  # ~280 km
    }
    client = FakeClient(
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
    assert titles["Folk Fest"]["bucket"] == "daytrip" and 140 < titles["Folk Fest"]["distance_km"] < 150
    assert result.searches == 3  # travel bucket has no interests
    assert abs(result.cost - 0.032) < 1e-9
    assert result.buckets_scanned == ["nearby", "local", "daytrip"]
    # every search prompt carries today's date
    assert all("Thursday, October 1, 2026" in c["messages"][-1]["content"] for c in client.calls)


async def test_one_failed_search_does_not_sink_the_run():
    geocache = {"somewhere, edmonton, alberta, canada": [53.55, -113.50]}
    client = FakeClient({"50": [ev("Indie Band Live", "2026-10-05T20:00", "Edmonton", "indie concerts")]}, fail_buckets=("nearby",))
    result = await run_pipeline(client, geocache, ("nearby", "local"))
    assert [e["title"] for e in result.events] == ["Indie Band Live"]
    assert len(result.errors) == 1 and "Nearby" in result.errors[0]


async def test_estimate_used_when_geocoding_fails():
    geocache = {"nowhere, edmonton, alberta, canada": None, "edmonton, alberta, canada": None}
    near = ev("Close show", "2026-10-05", "Edmonton", "indie concerts", est=20) | {"venue": "Nowhere"}
    far = ev("Far show", "2026-10-05", "Edmonton", "indie concerts", est=300) | {"venue": "Nowhere"}
    result = await run_pipeline(FakeClient({"50": [near, far]}), geocache, ("local",))
    assert [e["title"] for e in result.events] == ["Close show"]
    assert result.events[0]["distance_source"] == "estimate"
