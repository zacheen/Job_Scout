"""Cheap pre-LLM filters: US location and keyword relevance."""
from __future__ import annotations

from .models import Job


class LocationFilter:
    """Keeps roles whose location string looks US-based. Empty location excluded."""

    def __init__(self, us_terms: list[str]):
        self._terms = [t.lower() for t in us_terms]

    def is_us(self, job: Job) -> bool:
        loc = job.location.lower()
        if not loc:
            return False
        return any(term in loc for term in self._terms)


class KeywordFilter:
    """Pre-LLM gate: at least one keyword must appear in title or description."""

    def __init__(self, keywords: list[str]):
        self._keywords = [k.lower() for k in keywords]

    def matches(self, job: Job) -> bool:
        haystack = f"{job.title}\n{job.description}".lower()
        return any(kw in haystack for kw in self._keywords)
