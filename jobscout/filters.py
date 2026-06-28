"""Pre-scoring filter (drops unwanted jobs) and track routing."""
from __future__ import annotations

from .config import Track
from .models import Job


class PreFilter:
    """Initial gate applied before any scoring (keyword / CLI / API).

    `keep` returns True only if every rule passes. To add a rule later, write a
    private predicate and append it to `self._checks` — that's the only edit.
    """

    def __init__(self, us_terms: list[str], exclude_terms: list[str], exclude_dept_terms: list[str]):
        self._us_terms = [t.lower() for t in us_terms]
        self._exclude_terms = [t.lower() for t in exclude_terms]
        self._exclude_dept_terms = [t.lower() for t in exclude_dept_terms]
        # Department exclusion runs first (highest priority); all() short-circuits on it.
        self._checks = (self._dept_allowed, self._is_us, self._role_allowed)

    def keep(self, job: Job) -> bool:
        return all(check(job) for check in self._checks)

    def _dept_allowed(self, job: Job) -> bool:
        dept = job.department.lower()
        return not any(term in dept for term in self._exclude_dept_terms)

    def _is_us(self, job: Job) -> bool:
        # Empty/unknown location is treated as non-US and dropped.
        loc = job.location.lower()
        return bool(loc) and any(term in loc for term in self._us_terms)

    def _role_allowed(self, job: Job) -> bool:
        # Match each term within a single field (so a multi-word term can't span the
        # title/department join); covers e.g. department == "Sales".
        title, department = job.title.lower(), job.department.lower()
        return not any(t in title or t in department for t in self._exclude_terms)


class TrackRouter:
    """Assigns a job to the first matching track (title or description keyword scan).

    Order matters: put more specific tracks before broader ones in config.
    """

    def __init__(self, tracks: list[Track]):
        self._tracks = tracks

    def route(self, job: Job) -> Track | None:
        text = f"{job.title}\n{job.description}".lower()
        for track in self._tracks:
            if any(keyword in text for keyword in track.keywords):
                return track
        return None

    def ordered_names(self) -> list[str]:
        return [track.name for track in self._tracks]
