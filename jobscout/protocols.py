"""Structural interfaces (DIP). Implementations satisfy these by shape — no inheritance needed."""
from __future__ import annotations

from typing import Protocol

from .config import Track
from .models import Job, Score


class JobStore(Protocol):
    def is_seeded(self) -> bool: ...
    def known_uids(self) -> set[str]: ...
    def add_seen(self, job: Job) -> None: ...
    def set_score(self, job_uid: str, track: str, score: Score) -> None: ...
    def mark_emailed(self, job_uids: list[str]) -> None: ...
    def save(self) -> None: ...


class LocationMatcher(Protocol):
    def is_us(self, job: Job) -> bool: ...


class RoleMatcher(Protocol):
    def is_allowed(self, job: Job) -> bool: ...


class Router(Protocol):
    def route(self, job: Job) -> "Track | None": ...


class JobScorer(Protocol):
    def score(self, job: Job, track: Track) -> Score: ...


class Notifier(Protocol):
    def send_digest(self, items: list[tuple[Job, Score]], subject: str | None = None) -> None:
        """Send one digest. An empty `items` must be a no-op."""
        ...
