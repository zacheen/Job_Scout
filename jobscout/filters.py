"""Pre-scoring filter (drops unwanted jobs), description annotation, track routing, and level grouping."""
from __future__ import annotations

import re
from dataclasses import replace

from .config import Track
from .models import Job


def _word_re(terms: list[str]) -> re.Pattern | None:
    """Whole-word, case-insensitive alternation over `terms`; None (match nothing)
    when no non-blank terms — an empty-alternation regex would match everything."""
    terms = [t for t in terms if t.strip()]
    if not terms:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)


def _include_location_re(terms: list[str]) -> re.Pattern | None:
    r"""Location allowlist matcher: substring match, except a term shaped exactly ", xx"
    (comma-space + two letters) gets a trailing \b — else ", ca" substring-hits the start
    of ", Canada"/", Masovian" and leaks other countries. CAVEAT: the \b applies by shape
    alone; a 3-letter code or stray space silently falls back to an unprotected substring."""
    parts = []
    for t in terms:
        low = t.lower()
        if low.strip():
            parts.append(re.escape(low) + (r"\b" if re.fullmatch(r", [a-z]{2}", low) else ""))
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def _matches_any(pattern: re.Pattern | None, *texts: str) -> bool:
    """True if `pattern` is set and hits any of `texts`. `_word_re` / `_include_location_re` both
    return None for no terms, so folding that guard in here keeps callers from repeating the check."""
    return pattern is not None and any(pattern.search(t) for t in texts)


def _normalize_prose(text: str) -> str:
    """Lowercase free text, straighten curly apostrophes, and collapse whitespace runs —
    strip_html leaves multi-space/newline gaps where tags were, which would break
    multi-word phrase matching ("no  immigration\\nsponsorship")."""
    return " ".join(text.replace("’", "'").lower().split())


class PreFilter:
    """Initial gate applied before any scoring (keyword / CLI / API).

    `keep` returns True only if every rule passes. To add a rule later, write a
    private predicate and append it to `self._checks`; a rule needing new criteria
    data also threads that data through `__init__` (and Settings + wiring).
    """

    def __init__(self, *, include_location_terms: list[str], exclude_location_terms: list[str],
                 exclude_terms: list[str], exclude_dept_terms: list[str],
                 exclude_word_terms: list[str], exclude_description_terms: list[str]):
        self._include_location_re = _include_location_re(include_location_terms)
        # Whole-word backstop for the include allowlist: "india" drops ", India" but not
        # "Indiana"/"Indianapolis" (whole-word is safe — location is a structured place-name, not
        # free text). Even after that \b-fix, this still uniquely catches a "Remote, <foreign>"
        # role, which clears the include side on bare "remote".
        self._exclude_location_re = _word_re(exclude_location_terms)
        self._exclude_terms = [t.lower() for t in exclude_terms]
        self._exclude_dept_terms = [t.lower() for t in exclude_dept_terms]
        # Whole-word matcher for tokens too short to be safe substrings (e.g. "ux" must not hit "linux").
        self._exclude_word_re = _word_re(exclude_word_terms)
        # Normalized like the haystack so a term matches regardless of case/apostrophe/spacing.
        # Deliberately substring, NOT _word_re: "not sponsor" must also hit "cannot sponsor"
        # ("not" inside "cannot" has no \b boundary — rationale in config.yaml).
        self._exclude_description_terms = [_normalize_prose(t) for t in exclude_description_terms
                                           if t.strip()]
        # All pure predicates under all(), so this order only affects short-circuit speed, not the
        # result. Description scan goes last: it normalizes the longest text, and only after the
        # cheap title/dept/location checks failed to drop the job.
        self._checks = (self._dept_allowed, self._location_not_excluded, self._location_included,
                        self._role_allowed, self._role_words_allowed, self._description_allowed)

    def keep(self, job: Job) -> bool:
        return all(check(job) for check in self._checks)

    def _dept_allowed(self, job: Job) -> bool:
        dept = job.department.lower()
        return not any(term in dept for term in self._exclude_dept_terms)

    def _location_not_excluded(self, job: Job) -> bool:
        # Backstop for _location_included (short state codes / broad "remote" leak unwanted regions).
        return not _matches_any(self._exclude_location_re, job.location)

    def _location_included(self, job: Job) -> bool:
        # Empty/unknown location is treated as outside the allowed regions and dropped.
        return bool(job.location) and _matches_any(self._include_location_re, job.location)

    def _role_allowed(self, job: Job) -> bool:
        # Match within each field separately so a multi-word term can't span the title/department join.
        title, department = job.title.lower(), job.department.lower()
        return not any(t in title or t in department for t in self._exclude_terms)

    def _role_words_allowed(self, job: Job) -> bool:
        # Whole-word counterpart of _role_allowed for collision-prone short tokens.
        return not _matches_any(self._exclude_word_re, job.title, job.department)

    def _description_allowed(self, job: Job) -> bool:
        # "No visa sponsorship" boilerplate scan. Sources whose listing API omits the
        # description (Workday; Oracle has only a short blurb) pass through vacuously —
        # there is no text to match, here or at scoring time.
        if not self._exclude_description_terms or not job.description:
            return True
        description = _normalize_prose(job.description)
        return not any(term in description for term in self._exclude_description_terms)


class DescriptionFlagger:
    """Annotates (never drops) a job whose description matches a warn term, by returning
    a copy with `Job.note` set — the email renders the note as a caveat line.

    Companion to PreFilter's exclude_description_terms: that list is for phrasings that
    unambiguously mean "we don't sponsor"; this one is for AMBIGUOUS boilerplate like
    "authorized to work without sponsorship", which usually implies no sponsorship but
    sometimes just describes the candidate pool — so the role is still emailed, flagged.
    Same matching semantics as the exclude list (normalized substring).
    """

    def __init__(self, warn_description_terms: list[str],
                 note: str = "possibly NO visa sponsorship — description says {term!r}"):
        self._terms = [_normalize_prose(t) for t in warn_description_terms if t.strip()]
        self._note = note

    def annotate(self, job: Job) -> Job:
        if not self._terms or not job.description:
            return job
        description = _normalize_prose(job.description)
        term = next((t for t in self._terms if t in description), None)
        if term is None:
            return job
        return replace(job, note=self._note.format(term=term))


class TrackRouter:
    """Assigns a job to the FIRST track (config order) whose keyword hit count reaches
    its `min_hits`. A hit = one substring occurrence of one keyword in the title or the
    description; the SAME keyword occurring N times contributes N hits. A job reaching
    no track's min_hits is dropped. `ordered_names` = config order = email section order.

    Order matters: put more specific keyword tracks before broader ones in config.
    """

    def __init__(self, tracks: list[Track]):
        self._tracks = tracks

    def route(self, job: Job) -> Track | None:
        # Normalize each field separately (whitespace runs from strip_html would break
        # multi-word keywords like "computer vision"). Counting per field also means a
        # keyword can't match across the title/description boundary — same rule as
        # PreFilter._role_allowed.
        title = _normalize_prose(job.title)
        description = _normalize_prose(job.description)
        for track in self._tracks:
            # str.count is non-overlapping, e.g. "aa".count("aa") == 1, not 2.
            hits = sum(title.count(kw) + description.count(kw) for kw in track.keywords)
            if hits >= track.min_hits:
                return track
        return None

    def ordered_names(self) -> list[str]:
        return [track.name for track in self._tracks]


class LevelClassifier:
    """Top-level email grouping, first match wins (most important first): the referral
    group if the job's COMPANY is one the user has a referral at; else the intern group if
    the TITLE matches an intern/co-op term (whole-word, so "internal"/"international" don't
    count); else the senior group on a whole-word TITLE match (so "sr" doesn't hit inside
    other words) — referral outranks it, a senior role at a referral company stays in
    Referral; else the default group. `ordered_groups` = top-to-bottom order in the email;
    senior renders LAST (below default) since those roles are usually skimmed past.
    """

    def __init__(self, referral_companies: list[str], intern_terms: list[str],
                 senior_terms: list[str] = (),
                 referral_group: str = "Referral", intern_group: str = "Intern",
                 default_group: str = "Other roles", senior_group: str = "Senior"):
        self._referral = {c.strip().lower() for c in referral_companies if c.strip()}
        self._intern_re = _word_re(intern_terms)
        self._senior_re = _word_re(senior_terms)
        self._referral_group = referral_group
        self._intern_group = intern_group
        self._default_group = default_group
        self._senior_group = senior_group
        # Precompute the groups that can actually appear, in email order (top-to-bottom):
        # each conditional group only if it has match data, default always, senior last.
        groups = []
        if self._referral:
            groups.append(referral_group)
        if self._intern_re:
            groups.append(intern_group)
        groups.append(default_group)
        if self._senior_re:
            groups.append(senior_group)
        self._ordered_groups = tuple(groups)

    @property
    def senior_group(self) -> str:
        """Exposed so wiring (e.g. __main__) can key per-group scorer overrides on this name."""
        return self._senior_group

    def group(self, job: Job) -> str:
        if job.company.lower() in self._referral:
            return self._referral_group
        if _matches_any(self._intern_re, job.title):
            return self._intern_group
        if _matches_any(self._senior_re, job.title):
            return self._senior_group
        return self._default_group

    def ordered_groups(self) -> list[str]:
        return list(self._ordered_groups)
