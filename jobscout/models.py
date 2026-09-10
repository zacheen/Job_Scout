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
    `posted` — uid -> the ISO posting date THAT SOURCE recorded for it. Is that row still
    CURRENT? Per uid rather than per ledger row, because one row can carry uids from
    several sources that each report their own posting date (or none) — see
    `seen_snapshot` and store's source_dates column.
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
        """Newest first_seen date under `uid_prefix`, "" when nothing from this company
        reached the index (`has_rows` lists the ways that happens). Callers MUST read ""
        as "no cutoff known" and fall back to a weaker stop rule, never as "everything is
        old".
        """
        return self.watermarks.get(uid_prefix, "")

    def has_rows(self, uid_prefix: str) -> bool:
        """Has this source ever put a dated row in the ledger? Separates a source that
        BROKE from one that is merely new, which is what makes an empty pull worth a
        warning (ParallelFetcher) instead of being the normal first-run case.

        Reads the same index as `watermark`, so a source reads as new here whenever none
        of its rows made it into that index — every first_seen unparseable, or every uid
        predating the "{ats}:{company}:" format (store._uid_namespace). That direction is
        deliberate: it costs a missed warning, never a false one.
        """
        return uid_prefix in self.watermarks

    def seen_snapshot(self, uid: str, date_posted: str) -> bool:
        """Is this exact snapshot already recorded — uid known AND the posting date it
        arrives with matching the one recorded for that uid?

        Deliberately more than uid membership. A board that re-stamps an old role pushes it
        back to the top of the sort with a fresh posting date; treating that as "seen" lets
        a re-stamp burst satisfy the already-seen stop without the run having paged any
        deeper (Google did exactly this with 15 roles across its top two pages). A re-stamp
        is fresh board activity, so it is not evidence of depth — and it is also the signal
        that the stored row needs rewriting.

        A source carrying no posting date degrades to plain uid membership: weaker, but the
        only test available there. That covers a source with no date field at all AND one
        that merely omits it on this run, and the second case is why the test is on the
        ARRIVING date rather than on both sides matching as "". An omission tells us
        nothing about board activity, so calling it a re-stamp would rewrite the row and
        wipe its recorded date on every run, forever."""
        if uid not in self.uids:
            return False
        arriving = posted_iso(date_posted)
        return not arriving or self.posted.get(uid, "") == arriving


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


# Separator between a tier and the tool detail in a score_method. Private on purpose:
# `with_detail` and `tier_of` are the only two places allowed to know the format, which
# is what keeps scoring.py and store.py from each hard-coding their own half of it.
_METHOD_DETAIL_SEP = ":"


class ScoreMethod(StrEnum):
    """Which tier produced a score. A scorer reports it as `method_label` (the email
    subject) and CsvStore persists it as the `score_method` column, where
    store._score_rank reads it back as the merge-priority key.

    Those two readers are why this is one shared type and not a string in each module: a
    method the rank table has no key for silently falls to the lowest known rank, which
    would let a real LLM score LOSE a merge to a keyword one. StrEnum so a member needs no
    conversion to be written, formatted into a subject, or looked up by a raw string read
    back from CSV."""

    API = "API"
    CLI = "CLI"
    KEYWORD = "Keyword"

    def with_detail(self, detail: str) -> str:
        """`"CLI:agy"` — this tier plus which tool produced it. Returns a plain str,
        deliberately: the result is NOT a member, so only `tier_of` can read it back."""
        return f"{self}{_METHOD_DETAIL_SEP}{detail}"

    @staticmethod
    def tier_of(method: str) -> str:
        """The tier of a stored score_method, dropping any `with_detail` suffix. A str
        rather than a member because it also has to pass through the values no member
        covers — "" for a row written before the column existed, or a method some later
        version wrote."""
        return method.split(_METHOD_DETAIL_SEP, 1)[0]


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
    # The LLM read a hard work-authorization bar in the description (sponsorship refused,
    # US citizenship, security clearance). A BACKSTOP for phrasings PreFilter's
    # exclude_description_terms miss, so it only annotates the digest and deliberately
    # does NOT move experience_score — folding it in would make "strong fit but
    # ineligible" indistinguishable from "bad fit". Unlike the fields above it therefore
    # explains nothing about how experience_score was reached; it rides along on the same
    # LLM call as an eligibility caveat. Always False from KeywordScorer, which matches
    # keywords and cannot read a requirement out of prose. Not persisted (CsvStore's
    # _SCORE_FIELDS copy only the four scoring columns), same as `Job.note`.
    work_auth_barrier: bool = False

    @property
    def work_auth_caveat(self) -> str:
        """Display text for `work_auth_barrier`, "" when unset. Owned here rather than
        written at the call site so this flag matches `Job.note`, which already arrives
        as finished wording — otherwise the email would be the third place restating what
        the flag means, after the field above and the scorer's prompt."""
        if not self.work_auth_barrier:
            return ""
        return ("LLM read a work-authorization bar in this ad "
                "(sponsorship / citizenship / clearance)")
