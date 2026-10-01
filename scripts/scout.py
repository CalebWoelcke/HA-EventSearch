#!/usr/bin/env python3
"""Run the Event Scout search from a terminal, without Home Assistant.

Examples:
    export OPENROUTER_API_KEY=sk-or-...        (PowerShell: $env:OPENROUTER_API_KEY="sk-or-...")
    python scripts/scout.py --city Edmonton --region Alberta --country Canada \
        --interest "trivia nights:nearby" --interest "indie concerts:local" \
        --dislike "kids events"

    python scripts/scout.py --profile my_profile.json --json > results.json

Needs Python 3.11+ and ``pip install aiohttp``.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import aiohttp

# Load the integration's HA-free ``scout`` package by path. (Adding the integration
# folder to sys.path would let its calendar.py shadow the standard library.)
_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "local_event_scout" / "scout"
_spec = importlib.util.spec_from_file_location("scout", _PKG / "__init__.py", submodule_search_locations=[str(_PKG)])
sys.modules["scout"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sys.modules["scout"])

from scout.models import DEFAULT_BUCKETS, Interest, Location, Profile  # noqa: E402
from scout.openrouter import OpenRouterClient  # noqa: E402
from scout.pipeline import ScoutPipeline  # noqa: E402


def build_profile(args: argparse.Namespace) -> Profile:
    if args.profile:
        data = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        return Profile(
            locations=[Location(str(i), **loc) for i, loc in enumerate(data["locations"])],
            interests=[Interest(**i) for i in data["interests"]],
            dislikes=data.get("dislikes", []),
            min_score=data.get("min_score", 5),
            max_results=data.get("max_results", 8),
        )
    if not args.city or not args.interest:
        sys.exit("Give --city and at least one --interest, or --profile FILE.")
    interests = []
    for raw in args.interest:
        name, _, bucket = raw.rpartition(":") if ":" in raw else (raw, "", "local")
        interests.append(Interest(name.strip(), bucket.strip() or "local"))
    return Profile(
        locations=[Location("home", args.city, args.region, args.country)],
        interests=interests,
        dislikes=args.dislike or [],
        buckets=list(DEFAULT_BUCKETS),
        min_score=args.min_score,
        max_results=args.max_results,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="JSON file with locations, interests, dislikes")
    parser.add_argument("--city")
    parser.add_argument("--region", default="")
    parser.add_argument("--country", default="")
    parser.add_argument("--interest", action="append", help='"name:bucket", bucket = nearby|local|daytrip|travel')
    parser.add_argument("--dislike", action="append")
    parser.add_argument("--buckets", default="nearby,local,daytrip,travel", help="comma-separated buckets to search")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    parser.add_argument("--engine", default="auto", choices=["auto", "exa", "parallel", "native"])
    parser.add_argument("--timezone", default="America/Edmonton")
    parser.add_argument("--min-score", type=int, default=5)
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument("--no-links", action="store_true", help="skip link checking")
    parser.add_argument("--json", action="store_true", help="print raw JSON results")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("Set OPENROUTER_API_KEY first.")
    profile = build_profile(args)
    tz = ZoneInfo(args.timezone)

    async with aiohttp.ClientSession() as session:
        client = OpenRouterClient(session, api_key, app_title="Event Scout CLI")
        pipeline = ScoutPipeline(
            session, client, model=args.model, engine=args.engine, tz=tz, tz_name=args.timezone,
            check_links=not args.no_links,
        )
        result = await pipeline.run(
            profile,
            now=datetime.now(tz),
            bucket_ids=[b.strip() for b in args.buckets.split(",")],
            progress=lambda msg: print(f"  {msg}", file=sys.stderr),
        )

    events = sorted(result.events, key=lambda e: (-(e["score"] or 0), e["start"]))
    if args.json:
        print(json.dumps({"events": events, "cost": result.cost, "dropped": result.dropped, "errors": result.errors}, indent=2))
        return
    print(f"\n{len(events)} events, {result.searches} searches, cost ${result.cost:.4f}")
    print(f"Dropped: {result.dropped or 'none'}")
    for err in result.errors:
        print(f"! {err}")
    for e in events:
        if e["score"] is not None and e["score"] < profile.min_score:
            continue
        dist = f"{e['distance_km']:.0f} km" if e["distance_km"] is not None else "? km"
        print(f"\n[{e['score']}] {e['title']}\n    {e['start'][:16]} · {e['venue']}, {e['town']} · {dist} ({e['bucket']})")
        if e["why"]:
            print(f"    {e['why']}")
        print(f"    {e['url']}")


if __name__ == "__main__":
    asyncio.run(main())
