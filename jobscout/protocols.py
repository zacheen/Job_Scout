"""Structural interfaces (DIP). Implementations satisfy these by shape — no inheritance needed."""
from __future__ import annotations

from typing import Protocol

from .config import Track
from .models import Job, Score

# Ordered (track_name, [(job, score), ...]) sections for one grouped digest email.
Sections = list[tuple[str, list[tuple[Job, Score]]]]


class JobStore(Protocol):
    def is_seeded(self) -> bool: ...
    def known_uids(self) -> set[str]: ...
    def add_seen(self, job: Job) -> None: ...
    def set_score(self, job_uid: str, track: str, score: Score) -> None: ...
    def mark_emailed(self, job_uids: list[str]) -> None: ...
    def save(self) -> None: ...


class JobFilter(Protocol):
    def keep(self, job: Job) -> bool: ...


class Router(Protocol):
    def route(self, job: Job) -> "Track | None": ...
    def ordered_names(self) -> list[str]: ...


class JobScorer(Protocol):
    def score(self, job: Job, track: Track) -> Score: ...


class Notifier(Protocol):
    def send_digest(self, sections: Sections, subject: str | None = None) -> None:
        """Send one digest grouped into (track_name, items) sections. No items = no-op."""
        ...
