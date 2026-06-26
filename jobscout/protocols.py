"""Structural interfaces the Pipeline depends on (DIP).

Concrete implementations (CsvStore, Scorer, EmailNotifier, LocationFilter,
KeywordFilter) satisfy these by shape — no inheritance required.
"""
from __future__ import annotations

from typing import Protocol

from .models import Job, Score


class JobStore(Protocol):
    def is_seeded(self) -> bool: ...
    def known_uids(self) -> set[str]: ...
    def add_seen(self, job: Job) -> None: ...
    def set_score(self, job_uid: str, score: Score) -> None: ...
    def mark_emailed(self, job_uids: list[str]) -> None: ...
    def save(self) -> None: ...


class LocationMatcher(Protocol):
    def is_us(self, job: Job) -> bool: ...


class KeywordMatcher(Protocol):
    def matches(self, job: Job) -> bool: ...


class JobScorer(Protocol):
    def score(self, job: Job) -> Score: ...


class Notifier(Protocol):
    def send_digest(self, items: list[tuple[Job, Score]], subject: str | None = None) -> None: ...
