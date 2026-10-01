"""Parsing helpers for model output and event identity."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)
_STOPWORDS = {"the", "a", "an", "and", "of", "at", "in", "with", "live", "presents", "featuring", "feat"}


def extract_json(text: Any) -> Any:
    """Return the first JSON object or array found in model output.

    Handles code fences, leading prose and trailing commentary, which models
    add even when told not to.
    """
    if text is None:
        raise ValueError("Model returned no text.")
    if isinstance(text, (dict, list)):
        return text
    cleaned = _FENCE.sub("", str(text)).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            return value
    raise ValueError("No JSON found in model output.")


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", text.lower())
    kept = [w for w in words if w not in _STOPWORDS]
    return " ".join(kept or words)


def event_key(title: str, date_iso: str) -> str:
    """Stable identity from a normalised title and the event date (YYYY-MM-DD)."""
    raw = f"{normalize_title(title)}|{date_iso[:10]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def clean_str(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]
