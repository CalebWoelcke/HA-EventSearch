"""Event search pipeline with no Home Assistant dependencies.

Everything in this package can run on a normal PC (see scripts/scout.py), which
makes it quick to tune prompts and models without restarting Home Assistant.
Only relative imports are used inside the package so it can be imported either
as ``custom_components.local_event_scout.scout`` or as a top-level ``scout``.
"""

from .models import DEFAULT_BUCKETS, Bucket, Interest, Location, Profile, Source
from .pipeline import RunResult, ScoutPipeline
from .providers import GeminiProvider, OpenRouterProvider

__all__ = [
    "Bucket",
    "DEFAULT_BUCKETS",
    "GeminiProvider",
    "Interest",
    "Location",
    "OpenRouterProvider",
    "Profile",
    "RunResult",
    "ScoutPipeline",
    "Source",
]
