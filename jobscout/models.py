"""Immutable value objects passed between pipeline stages.

Side-effect-free with one exception: SeenLedger.seen_snapshot() normalizes a posting date
through dates.posted_iso(), which logs a diagnostic for a shape it cannot read. That
warning belongs to the normalization, and the alternative — normalizing in each caller
instead — is worse, because the callers must agree exactly (see seen_snapshot)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType

from .dates import posted_iso


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


@dataclass(frozen=True)
class DescriptionPolicy:
    """Does a listing's body carry enough text to be worth scoring?

    NOT the same question as "is it non-empty", and the difference is not academic. Some
    listing APIs answer with a marketing teaser instead of the job ad — IBM's careers
    index sends the same ~250-char paragraph on every role, cut off mid-sentence — and an
    emptiness test reads that as a real description. It then disables all three mechanisms
    that exist to carry a body-less posting: JdUrlEnricher skips the JD backfill,
    KeywordScorer drops from `_TITLE_ONLY_WEIGHT` to `_WEIGHT`, and TitleOnlyAutoPass
    stops firing. So a teaser scores STRICTLY LOWER than no description at all — measured
    on IBM "Software Developer Spring Co-op 2027": 49 with the teaser, 56 on the bare
    title, 91 on the real body.

    `min_chars` is a floor on substance, not a quality judgement. A body too short to hold
    a requirements list cannot move a keyword count either way, so calling it absent only
    ever loosens the gate — the deliberate direction, since a missed role costs more than
    a re-read one.
    """

    min_chars: int
    # A body ending mid-sentence is a search-index snippet at ANY length, so the floor
    # alone is not enough (IBM's teaser clears any floor set below ~250). A tuple, not a
    # list, because str.endswith rejects a list. Empty disables the rule.
    truncation_marks: tuple[str, ...]

    def is_usable(self, description: str) -> bool:
        text = description.strip()
        return len(text) >= self.min_chars and not text.endswith(self.truncation_marks)


# eq=False: frozen+eq would synthesise a __hash__ over `watermarks`, which raises
# TypeError on any dict. Nothing compares ledgers by value, so keep object identity.
@dataclass(frozen=True, eq=False)
class SeenLedger:
    """What a fetcher is allowed to know about the ledger. Two different questions, two
    different keys — see `seen_snapshot` for why they are not the same test:

    `uids` — which source uids exist at all. IS this opening one we have a row for?
    `posted` — uid -> the ISO posting date recorded for it. Is that row still CURRENT?
    `watermarks` — uid prefix (AtsFetcher.uid_prefix) -> newest first_seen date
    (YYYY-MM-DD) under it. Keyed by uid prefix rather than company name because ledger rows
    carry a display name that aliasing can rewrite, while uids keep the fetcher's own
    namespace.
    """

    uids: frozenset[str]
    watermarks: Mapping[str, str]
    posted: Mapping[str, str]

    def __post_init__(self) -> None:
        # frozen=True only blocks rebinding the field, not mutating the dict behind it.
        # One instance (EMPTY_SEEN_LEDGER) is the shared default of every fetch() and is
        # read by concurrent host threads, so a single stray write would leak across
        # companies and threads. Copy + proxy makes that impossible, not just impolite.
        for field in ("watermarks", "posted"):
            object.__setattr__(self, field, MappingProxyType(dict(getattr(self, field))))

    def watermark(self, uid_prefix: str) -> str:
        """Newest first_seen date under `uid_prefix`, "" when this company has no rows yet
        (a seed run) or its dates are unparseable. Callers MUST read "" as "no cutoff
        known" and fall back to a weaker stop rule, never as "everything is old".
        """
        return self.watermarks.get(uid_prefix, "")

    def seen_snapshot(self, uid: str, date_posted: str) -> bool:
        """Is this exact snapshot already recorded — uid known AND the posting date it
        arrives with matching the one stored for that uid?

        Deliberately more than uid membership. A board that re-stamps an old role pushes it
        back to the top of the sort with a fresh posting date; treating that as "seen" lets
        a re-stamp burst satisfy the already-seen stop without the run having paged any
        deeper (Google did exactly this with 15 roles across its top two pages). A re-stamp
        is fresh board activity, so it is not evidence of depth — and it is also the signal
        that the stored row needs rewriting.

        A source carrying no posting date compares "" to "" and so degrades to plain uid
        membership: weaker, but the only test available there."""
        return uid in self.uids and self.posted.get(uid, "") == posted_iso(date_posted)


# Shared no-ledger default: a fetch with no dedupe context (seed run, ad-hoc probe).
EMPTY_SEEN_LEDGER = SeenLedger(frozenset(), {}, {})


class ScoreScale(StrEnum):
    """Which arithmetic produced an `experience_score`, and so which Track threshold
    gates it. Scores from different scales are NOT comparable: LLM is a resume-fit
    judgement over the full 0-100 range, KEYWORD is `40 + weight * distinct
    skill_keywords/title_keywords matched`, which cannot leave 40-100 and says nothing
    about fit."""

    LLM = "llm"
    KEYWORD = "keyword"


@dataclass(frozen=True)
class Score:
    experience_score: int   # meaning depends on `scale`; see ScoreScale
    reason: str
    # Required (no default) so no scorer can leave the score's meaning implicit; the
    # email gate reads this to pick the Track threshold.
    scale: ScoreScale
    # Distinct skill_keywords (scanned over title+description) plus title_keywords
    # (title only) matched; set only by KeywordScorer with either list configured (None
    # for LLM scorers and the no-keywords path). Needed because the clamped
    # experience_score can saturate at 100 — email and section sort use this instead.
    matches: int | None = None
    # Per-keyword breakdown behind `matches`: (keyword, occurrences in the job text),
    # ordered by count desc. Same None semantics as `matches`; email display only.
    match_counts: tuple[tuple[str, int], ...] | None = None
    # The title_keywords half of `matches`, kept apart from match_counts so the email can
    # mark it as the weaker signal: a role noun says the title is technical, not that the
    # candidate fits. Disjoint from match_counts (KeywordScorer guarantees it).
    title_match_counts: tuple[tuple[str, int], ...] | None = None
