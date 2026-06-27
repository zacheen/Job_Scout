"""Pre-scoring gates: US location, non-technical role exclusion, and track routing."""
from __future__ import annotations

from .config import Track
from .models import Job


class LocationFilter:
    """Keeps roles whose location string looks US-based. Empty location excluded."""

    def __init__(self, us_terms: list[str]):
        self._terms = [t.lower() for t in us_terms]

    def is_us(self, job: Job) -> bool:
        loc = job.location.lower()
        return bool(loc) and any(term in loc for term in self._terms)


class RoleFilter:
    """Drops clearly non-technical roles by title (marketing, sales, support, ...)."""

    def __init__(self, exclude_title_terms: list[str]):
        self._terms = [t.lower() for t in exclude_title_terms]

    def is_allowed(self, job: Job) -> bool:
        title = job.title.lower()
        return not any(term in title for term in self._terms)


class TrackRouter:
    """Assigns a job to the first track whose keywords appear in title or description.
    Order matters: list more specific tracks first in config."""

    def __init__(self, tracks: list[Track]):
        self._tracks = tracks

    def route(self, job: Job) -> Track | None:
        text = f"{job.title}\n{job.description}".lower()
        for track in self._tracks:
            if any(keyword in text for keyword in track.keywords):
                return track
        return None
