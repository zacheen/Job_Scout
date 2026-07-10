"""Structural interfaces (DIP). Implementations satisfy these by shape — no inheritance needed."""
from __future__ import annotations

from collections.abc import Collection
from typing import ClassVar, Protocol

from .config import Track
from .models import Job, Score

# A group's ordered track sections: (track_name, ranked [(job, score), ...]).
Sections = list[tuple[str, list[tuple[Job, Score]]]]
# Two-level digest: ordered (group_name, that group's track sections) for one email.
Digest = list[tuple[str, Sections]]


class Fetcher(Protocol):
    def fetch_all(self, seen: Collection[str]) -> list[Job]:
        """Fetch all current postings (an impl may run per-host in parallel). `seen` lets
        date-ordered sources stop paginating early; dedup stays the pipeline's job."""
        ...


class JobStore(Protocol):
    def is_seeded(self) -> bool: ...
    def known_uids(self) -> set[str]: ...
    def known_urls(self) -> set[str]: ...  # for cross-source email dedup (by URL, not uid)
    def add_seen(self, job: Job) -> None: ...
    def set_score(self, job_uid: str, track: str, score: Score, method: str = "") -> None: ...
    def mark_emailed(self, job_uids: list[str]) -> None: ...
    def save(self) -> None: ...


class JobFilter(Protocol):
    def keep(self, job: Job) -> bool: ...


class Annotator(Protocol):
    def annotate(self, job: Job) -> Job:
        """Return the job, or a copy with derived presentation fields (e.g. `note`) set.
        Must not change identity fields (job_uid/url) — dedup keys on them."""
        ...


class Router(Protocol):
    def route(self, job: Job) -> "Track | None": ...
    def ordered_names(self) -> list[str]: ...


class Leveler(Protocol):
    def group(self, job: Job) -> str: ...
    def ordered_groups(self) -> list[str]: ...  # listed top-to-bottom in the email


class JobScorer(Protocol):
    method_label: ClassVar[str]  # scoring method shown in the email subject, e.g. "API" / "CLI" / "Keyword"

    def score(self, job: Job, track: Track) -> Score: ...


class Notifier(Protocol):
    def send_digest(self, digest: Digest, subject: str | None = None) -> None:
        """Send one email grouped two levels (group -> track sections). No items = no-op."""
        ...
