"""Plain data types shared by the pipeline and the Home Assistant glue."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Bucket:
    """A distance band that interests are assigned to."""

    id: str
    label: str
    km: float
    lookahead_days: int
    refresh_days: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any], default: "Bucket") -> "Bucket":
        def _num(key: str, cast, lo, hi):
            try:
                return max(lo, min(hi, cast(raw.get(key, getattr(default, key)))))
            except (TypeError, ValueError):
                return getattr(default, key)

        return cls(
            id=default.id,
            label=str(raw.get("label") or default.label).strip()[:40] or default.label,
            km=_num("km", float, 1, 2000),
            lookahead_days=_num("lookahead_days", int, 1, 180),
            refresh_days=_num("refresh_days", int, 1, 30),
        )


DEFAULT_BUCKETS: list[Bucket] = [
    Bucket("nearby", "Nearby", 15, 7, 1),
    Bucket("local", "Local", 50, 14, 1),
    Bucket("daytrip", "Day trip", 150, 30, 3),
    Bucket("travel", "Worth travelling", 400, 90, 7),
]
DEFAULT_BUCKET_ID = "local"


PRIORITIES = ("high", "normal", "low")
PRIORITY_BOOST = {"high": 1, "normal": 0, "low": -1}


@dataclass
class Interest:
    name: str
    bucket: str = DEFAULT_BUCKET_ID
    priority: str = "normal"


@dataclass
class Source:
    """A page that lists events (venue calendar, organiser's "what's on" page)."""

    url: str
    bucket: str = DEFAULT_BUCKET_ID
    name: str = ""


@dataclass
class Location:
    id: str
    city: str
    region: str = ""
    country: str = ""
    lat: float | None = None
    lon: float | None = None

    @property
    def text(self) -> str:
        return ", ".join(p for p in (self.city, self.region, self.country) if p)


@dataclass
class Profile:
    locations: list[Location]
    interests: list[Interest]
    dislikes: list[str] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=lambda: list(DEFAULT_BUCKETS))
    min_score: int = 5
    max_results: int = 8

    def bucket(self, bucket_id: str) -> Bucket:
        for bucket in self.buckets:
            if bucket.id == bucket_id:
                return bucket
        return next(b for b in self.buckets if b.id == DEFAULT_BUCKET_ID)

    def interests_for(self, bucket_id: str) -> list[Interest]:
        return [i for i in self.interests if i.bucket == bucket_id]


def to_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)
