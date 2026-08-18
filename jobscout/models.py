"""Immutable value objects passed between pipeline stages."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType


@dataclass(frozen=True)
class Job:
    # "{ats}:{company}:{ats_job_id}" — dedupe key across all stores.
    job_uid: str
    company: str
    title: str
    location: str
    url: str
    description: str
    department: str = ""
    date_posted: str = ""
    # Pipeline-derived caveat shown in the email (e.g. "possibly no visa sponsorship");
    # never persisted (CsvStore's fixed _FIELDS ignore it).
    note: str = ""
    # Readable stand-in for `location`, populated only by WorkdayFetcher's multi-site
    # fallback when the raw value is a URL slug. Same display-only contract as `note`
    # (not persisted); read via `display_location`, never directly.
    location_display: str = ""

    def __post_init__(self):
        # ATS JSON can carry explicit nulls (e.g. "department": null) that
        # item.get(key, "") won't catch since the key exists — None then
        # crashes downstream .lower()/regex. Coerce here once for every fetcher.
        for f in fields(self):
            if getattr(self, f.name) is None:
                object.__setattr__(self, f.name, "")

    @property
    def display_location(self) -> str:
        """`location` for humans (email) and the LLM prompt. Presentation only — never feed this
        to PreFilter, whose state-code rule needs the raw separator ("McLean-VA")."""
        return self.location_display or self.location


# eq=False: frozen+eq would synthesise a __hash__ over `watermarks`, which raises
# TypeError on any dict. Nothing compares ledgers by value, so keep object identity.
@dataclass(frozen=True, eq=False)
class SeenLedger:
    """What a fetcher is allowed to know about the ledger: which source uids are already
    recorded (`uids`, the membership test for "seen"), and how CURRENT those records are.

    `watermarks` maps uid prefix (AtsFetcher.uid_prefix) -> newest first_seen date
    (YYYY-MM-DD) recorded under it. Keyed by uid prefix rather than company name because
    ledger rows carry a display name that aliasing can rewrite, while uids keep the
    fetcher's own namespace.
    """

    uids: frozenset[str]
    watermarks: Mapping[str, str]

    def __post_init__(self) -> None:
        # frozen=True only blocks rebinding the field, not mutating the dict behind it.
        # One instance (EMPTY_SEEN_LEDGER) is the shared default of every fetch() and is
        # read by concurrent host threads, so a single stray write would leak across
        # companies and threads. Copy + proxy makes that impossible, not just impolite.
        object.__setattr__(self, "watermarks", MappingProxyType(dict(self.watermarks)))

    def watermark(self, uid_prefix: str) -> str:
        """Newest first_seen date under `uid_prefix`, "" when this company has no rows yet
        (a seed run) or its dates are unparseable. Callers MUST read "" as "no cutoff
        known" and fall back to a weaker stop rule, never as "everything is old".
        """
        return self.watermarks.get(uid_prefix, "")


# Shared no-ledger default: a fetch with no dedupe context (seed run, ad-hoc probe).
EMPTY_SEEN_LEDGER = SeenLedger(frozenset(), {})


class ScoreScale(StrEnum):
    """Which arithmetic produced an `experience_score`, and so which Track threshold
    gates it. Scores from different scales are NOT comparable: LLM is a resume-fit
    judgement over the full 0-100 range, KEYWORD is `40 + 3 * distinct skill_keywords
    matched`, which cannot leave 40-100 and says nothing about fit."""

    LLM = "llm"
    KEYWORD = "keyword"


@dataclass(frozen=True)
class Score:
    experience_score: int   # meaning depends on `scale`; see ScoreScale
    reason: str
    # Required (no default) so no scorer can leave the score's meaning implicit; the
    # email gate reads this to pick the Track threshold.
    scale: ScoreScale
    # Distinct skill_keywords matched; set only by KeywordScorer with keywords configured
    # (None for LLM scorers and the no-keywords path). Needed because the clamped
    # experience_score can saturate at 100 — email and section sort use this instead.
    matches: int | None = None
    # Per-keyword breakdown behind `matches`: (keyword, occurrences in the job text),
    # ordered by count desc. Same None semantics as `matches`; email display only.
    match_counts: tuple[tuple[str, int], ...] | None = None
