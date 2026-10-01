"""Prompt builders. Kept separate so they are easy to tune from the CLI."""

from __future__ import annotations

from datetime import date, timedelta
import json
from typing import Any

from .models import Bucket, Interest

SEARCH_SYSTEM = (
    "You find real, upcoming in-person events using web search. You never invent events, "
    "dates or links: every event you return must appear in your search results. "
    "You answer with a single JSON object and nothing else."
)

RANK_SYSTEM = (
    "You are a careful personal events curator. You score how well events match one "
    "person's tastes. You answer with a single JSON object and nothing else."
)


def search_prompt(
    *,
    today: date,
    tz_name: str,
    location_text: str,
    bucket: Bucket,
    interests: list[Interest],
    dislikes: list[str],
    max_results: int,
) -> str:
    end = today + timedelta(days=bucket.lookahead_days)
    interest_lines = "\n".join(f"- {i.name}" for i in interests)
    dislike_text = ", ".join(dislikes) if dislikes else "nothing specified"
    return f"""Today is {today:%A, %B} {today.day}, {today.year} (time zone {tz_name}).

Find upcoming in-person events within about {bucket.km:g} km of {location_text} that start
between {today.isoformat()} and {end.isoformat()} (inclusive), matching these interests:
{interest_lines}

Skip anything matching: {dislike_text}.
Also skip online-only events, recurring weekly specials (happy hours, wing nights) unless an
interest asks for them, and anything whose date you cannot confirm from a source.
Prefer the official event, venue or ticketing page as the URL. Search more than once if the
first results are thin, but return at most {max_results} events.

Return exactly this JSON shape:
{{"events": [{{
  "title": "event name",
  "start": "YYYY-MM-DD or YYYY-MM-DDTHH:MM local time",
  "end": "same format, or empty string if unknown",
  "venue": "venue name",
  "address": "street address if known, else empty",
  "town": "town or city",
  "url": "https://... page that lists this event",
  "category": "short category, e.g. live music, trivia, festival",
  "interest": "which listed interest it matches, copied exactly",
  "outdoor": true or false,
  "est_distance_km": approximate distance from {location_text} as a number,
  "summary": "one sentence describing the event"
}}]}}
If nothing qualifies, return {{"events": []}}."""


def rank_prompt(
    *,
    today: date,
    interests: list[Interest],
    bucket_labels: dict[str, str],
    dislikes: list[str],
    liked: list[str],
    disliked: list[str],
    weather: str,
    candidates: list[dict[str, Any]],
) -> str:
    interest_text = "\n".join(f"- {i.name} ({bucket_labels.get(i.bucket, i.bucket)})" for i in interests)
    feedback = ""
    if liked:
        feedback += "\nEvents they marked as interesting before:\n" + "\n".join(f"- {t}" for t in liked)
    if disliked:
        feedback += "\nEvents they marked as not interesting before:\n" + "\n".join(f"- {t}" for t in disliked)
    weather_text = (
        f"\nWeather forecast near home:\n{weather}\n"
        "For outdoor events on days with bad weather (heavy rain or snow, extreme cold or heat, "
        "storms), add a short weather_note and lower the score by 1-2."
        if weather
        else ""
    )
    return f"""Today is {today:%A, %B} {today.day}, {today.year}.

The person's interests:
{interest_text}

They want to avoid: {", ".join(dislikes) if dislikes else "nothing specified"}.{feedback}
{weather_text}

Score each candidate event from 0 to 10 for how likely this person is to want to go:
10 = an exact match they would hate to miss, 7 = clearly matches an interest,
5 = loosely related, 2 = weak match, 0 = matches something they avoid.
Use their past feedback as the strongest signal of taste.

Candidates:
{json.dumps(candidates, ensure_ascii=False)}

Return exactly:
{{"rankings": [{{"id": "candidate id", "score": 0-10, "why": "max 20 words, addressed to them", "weather_note": "empty unless weather matters"}}]}}
Include every candidate id once."""
