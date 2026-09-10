"""Structural interfaces (DIP). Implementations satisfy these by shape — no inheritance needed."""
from __future__ import annotations

from typing import ClassVar, Protocol

from .config import Track
from .models import Job, Score, ScoreScale, SeenLedger

# A group's ordered track sections: (track_name, ranked [(job, score), ...]).
Sections = list[tuple[str, list[tuple[Job, Score]]]]
# Two-level digest: ordered (group_name, that group's track sections) for one email.
Digest = list[tuple[str, Sections]]


class Fetcher(Protocol):
    def fetch_all(self, seen: SeenLedger) -> list[Job]:
        """Fetch all current postings (an impl may run per-host in parallel). `seen` lets
        date-ordered sources stop paginating early; dedup stays the pipeline's job."""
        ...


class JobStore(Protocol):
    def is_seeded(self) -> bool: ...

    def seen_ledger(self) -> SeenLedger:
        """Every recorded source uid plus per-company recency, for fetchers that bound
        pagination by date. MUST reflect the ledger BEFORE this run's add_seen calls, or a
        company's watermark advances to today and its pull stops at the first page."""
        ...

    def known_urls(self) -> set[str]:
        """Cross-source email dedup keys. MUST be canonical (urls.canon_url) —
        callers compare via canon_url(url), never a raw URL string."""
        ...
    def add_seen(self, job: Job) -> None: ...
    def set_score(self, job_uid: str, track: str, score: Score, method: str = "") -> None: ...

    def mark_emailed(self, job_uids: list[str]) -> None:
        """Flag these uids as emailed. MUST NOT raise on an unknown one — callers run this
        AFTER the digest is sent and call save() straight after, so raising skips save()
        and every role in the run re-emails next time. Log and skip instead. set_score has
        the opposite contract precisely because it runs BEFORE the send."""
        ...

    def save(self) -> None: ...


class JobFilter(Protocol):
    def keep(self, job: Job) -> bool: ...


class Annotator(Protocol):
    def annotate(self, job: Job) -> Job:
        """Return the job, or a copy with derived presentation fields (e.g. `note`) set.
        Must not change identity fields (job_uid/url) — dedup keys on them."""
        ...


class Enricher(Protocol):
    def enrich(self, job: Job) -> Job:
        """Return the job, or a copy with fields the listing API omitted (e.g. the full
        description) filled from a per-job detail request. Must not change identity fields
        (job_uid/url), and must fail open — on a fetch error, return `job` unchanged.
        Return `job` ITSELF (same object) when nothing was fetched — the pipeline keys
        "skip the redundant re-filter" on object identity."""
        ...


class Router(Protocol):
    def route(self, job: Job) -> "Track | None": ...
    def ordered_names(self) -> list[str]: ...


class Leveler(Protocol):
    def group(self, job: Job) -> str: ...
    def ordered_groups(self) -> list[str]: ...  # listed top-to-bottom in the email


class JobScorer(Protocol):
    # Scoring method, shown in the email subject and persisted as the ledger's
    # score_method: "API" / "Keyword" / "CLI:<tool>". NOT a ClassVar — CliScorer and
    # TitleOnlyAutoPass both compute it per instance.
    method_label: str
    scale: ClassVar[ScoreScale]  # picks which Track threshold gates this scorer output

    def score(self, job: Job) -> Score: ...


class Notifier(Protocol):
    def send_digest(self, digest: Digest, subject: str | None = None) -> None:
        """Send one email grouped two levels (group -> track sections). No items = no-op."""
        ...
