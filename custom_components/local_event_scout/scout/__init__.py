"""Event search pipeline with no Home Assistant dependencies.

Everything in this package can run on a normal PC (see scripts/scout.py), which
makes it quick to tune prompts and models without restarting Home Assistant.
Only relative imports are used inside the package so it can be imported either
as ``custom_components.local_event_scout.scout`` or as a top-level ``scout``.
"""

from .models import Bucket, Interest, Location, Profile, DEFAULT_BUCKETS
from .pipeline import RunResult, ScoutPipeline

__all__ = [
    "Bucket",
    "DEFAULT_BUCKETS",
    "Interest",
    "Location",
    "Profile",
    "RunResult",
    "ScoutPipeline",
]
