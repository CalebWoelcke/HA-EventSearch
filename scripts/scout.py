#!/usr/bin/env python3
"""Run the Event Scout search from a terminal, without Home Assistant.

OpenRouter:
    export OPENROUTER_API_KEY=sk-or-...        (PowerShell: $env:OPENROUTER_API_KEY="sk-or-...")
    python scripts/scout.py --city Edmonton --region Alberta --country Canada \
        --interest "trivia nights:nearby:high" --interest "indie concerts:local" --dislike "kids events"

Gemini (Google AI Studio):
    export GEMINI_SEARCH_KEY=...   # key from a project with billing (Google Search grounding)
    export GEMINI_KEY=...          # optional free-tier key for everything else
    python scripts/scout.py --provider gemini --city Edmonton ...

Saved sources (read directly; add --no-search to skip the web search):
    python scripts/scout.py ... --source "https://venue.example/events:local" --no-search

Interests are "name[:bucket[:priority]]"; bucket = nearby|local|daytrip|travel, priority = high|normal|low.
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

from scout.models import DEFAULT_BUCKETS, PRIORITIES, Interest, Location, Profile, Source  # noqa: E402
from scout.openrouter import OpenRouterClient  # noqa: E402
from scout.pipeline import ScoutPipeline  # noqa: E402
from scout.providers import GeminiProvider, OpenRouterProvider  # noqa: E402


def parse_interest(raw: str) -> Interest:
    parts = [p.strip() for p in raw.split(":")]
    priority = parts.pop() if len(parts) > 2 and parts[-1] in PRIORITIES else "normal"
    bucket = parts.pop() if len(parts) > 1 and parts[-1] in {b.id for b in DEFAULT_BUCKETS} else "local"
    return Interest(":".join(parts), bucket, priority)


def parse_source(raw: str) -> Source:
    url, _, bucket = raw.rpartition(":")
    if bucket in {b.id for b in DEFAULT_BUCKETS}:
        return Source(url, bucket)
    return Source(raw, "local")


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
    return Profile(
        locations=[Location("home", args.city, args.region, args.country)],
        interests=[parse_interest(i) for i in args.interest],
        dislikes=args.dislike or [],
        buckets=list(DEFAULT_BUCKETS),
        min_score=args.min_score,
        max_results=args.max_results,
    )


def build_provider(args: argparse.Namespace, session: aiohttp.ClientSession):
    if args.provider == "gemini":
        search_key = os.environ.get("GEMINI_SEARCH_KEY") or os.environ.get("GEMINI_KEY")
        if not search_key:
            sys.exit("Set GEMINI_SEARCH_KEY (billing-enabled project) and optionally GEMINI_KEY (free tier).")
        return GeminiProvider(
            session,
            search_key=search_key,
            process_key=os.environ.get("GEMINI_KEY"),
            search_model=args.search_model or "gemini-3.1-flash-lite",
            process_model=args.model or "gemini-3.8-flash",
        )
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("Set OPENROUTER_API_KEY first.")
    client = OpenRouterClient(session, api_key, app_title="Event Scout CLI")
    return OpenRouterProvider(client, args.model or "deepseek/deepseek-v4.1-flash", args.engine)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="JSON file with locations, interests, dislikes")
    parser.add_argument("--city")
    parser.add_argument("--region", default="")
    parser.add_argument("--country", default="")
    parser.add_argument("--interest", action="append", help='"name[:bucket[:priority]]"')
    parser.add_argument("--dislike", action="append")
    parser.add_argument("--source", action="append", help='saved source page, "url[:bucket]"')
    parser.add_argument("--no-search", action="store_true", help="only read --source pages, no web search")
    parser.add_argument("--buckets", default="nearby,local,daytrip,travel", help="comma-separated buckets to process")
    parser.add_argument("--provider", default="openrouter", choices=["openrouter", "gemini"])
    parser.add_argument("--model", help="OpenRouter model, or the Gemini model for non-search calls")
    parser.add_argument("--search-model", help="Gemini model for search calls")
    parser.add_argument("--engine", default="auto", choices=["auto", "exa", "parallel", "native"])
    parser.add_argument("--timezone", default="America/Edmonton")
    parser.add_argument("--min-score", type=int, default=5)
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument("--no-links", action="store_true", help="skip link checking")
    parser.add_argument("--json", action="store_true", help="print raw JSON results")
    args = parser.parse_args()

    profile = build_profile(args)
    tz = ZoneInfo(args.timezone)
    bucket_ids = [b.strip() for b in args.buckets.split(",")]

    async with aiohttp.ClientSession() as session:
        pipeline = ScoutPipeline(
            session, build_provider(args, session), tz=tz, tz_name=args.timezone, check_links=not args.no_links
        )
        result = await pipeline.run(
            profile,
            now=datetime.now(tz),
            bucket_ids=bucket_ids,
            search_bucket_ids=[] if args.no_search else bucket_ids,
            sources=[parse_source(s) for s in args.source or []],
            progress=lambda msg: print(f"  {msg}", file=sys.stderr),
        )

    events = sorted(result.events, key=lambda e: (-(e["score"] or 0), e["start"]))
    if args.json:
        print(json.dumps({
            "events": events, "cost": result.cost, "calls": result.calls, "dropped": result.dropped,
            "errors": result.errors, "source_stats": result.source_stats, "suggested_sources": result.suggested_sources,
        }, indent=2))
        return
    print(f"\n{len(events)} events · {result.searches} search calls ({result.web_searches} web searches) · "
          f"{result.pages_read} pages read · cost ${result.cost:.4f}")
    for call in result.calls:
        print(f"  {call['kind']:<6} {call['label'][:48]:<48} ${call.get('cost') or 0:.4f}  in {call.get('input_tokens')} "
              f"out {call.get('output_tokens')} (thinking {call.get('reasoning_tokens')})  searches {call.get('web_searches')}")
    print(f"Dropped: {result.dropped or 'none'}")
    for err in result.errors:
        print(f"! {err}")
    for url, stats in result.source_stats.items():
        print(f"  source {url}: {stats['found']} events{' — ' + stats['error'] if stats['error'] else ''}")
    for suggestion in result.suggested_sources:
        print(f"  suggested source: {suggestion['url']} ({suggestion['bucket']})")
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
