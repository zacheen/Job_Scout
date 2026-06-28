"""Pre-scoring filter (drops unwanted jobs), track routing, and level grouping."""
from __future__ import annotations

import re

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
        # Department exclusion runs first (highest priority).
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
        # Match within each field separately so a multi-word term can't span the title/department join.
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


class LevelClassifier:
    """Top-level email grouping, first match wins (most important first): the referral
    group if the job's COMPANY is one the user has a referral at; else the intern group if
    the TITLE matches an intern/co-op term (whole-word, so "internal"/"international" don't
    count); else the default group. `ordered_groups` = top-to-bottom order in the email.
    """

    def __init__(self, referral_companies: list[str], intern_terms: list[str],
                 referral_group: str = "Referral", intern_group: str = "Intern",
                 default_group: str = "Other roles"):
        self._referral = {c.strip().lower() for c in referral_companies if c.strip()}
        terms = [t for t in intern_terms if t.strip()]
        # None (not an empty-matching regex) when no terms, so nothing is tagged intern.
        self._intern_re = (
            re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
            if terms else None
        )
        self._referral_group = referral_group
        self._intern_group = intern_group
        self._default_group = default_group
        # Precompute the groups that can actually appear, in email order (top-to-bottom):
        # referral only if any referral companies, intern only if any terms, default always.
        groups = []
        if self._referral:
            groups.append(referral_group)
        if self._intern_re:
            groups.append(intern_group)
        groups.append(default_group)
        self._ordered_groups = tuple(groups)

    def group(self, job: Job) -> str:
        if job.company.lower() in self._referral:
            return self._referral_group
        if self._intern_re and self._intern_re.search(job.title):
            return self._intern_group
        return self._default_group

    def ordered_groups(self) -> list[str]:
        return list(self._ordered_groups)
