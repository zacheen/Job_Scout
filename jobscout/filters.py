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


def _region_matcher(terms: list[str], *, allow_state_codes: bool = True) -> re.Pattern | None:
    r"""Allowlist matcher for US/Taiwan location tokens. Per term:
      - ", xx" (comma-space + two letters) = a US state code: matches after a comma OR dash
        separator with a trailing \b ("Boston, MA" / "Remote - CA", but not ", Canada"/", Masovian").
        Skipped when allow_state_codes=False (for titles, where ", or"/", in" would hit "or"/"in").
      - a bare 2-3 letter token ("us"/"usa"): whole-word, so "us" won't hit "Belarus"/"Houston".
      - anything longer: raw substring (", CA, USA" still contains "usa").
    CAVEAT: the state-code rule keys on SHAPE; a 3-letter code or a stray space silently degrades to
    a substring — harmless for long names, but for a 2-letter code it reintroduces the prefix leak.
    KNOWN edge: a 2-letter code can coincide with a foreign admin-region abbreviation — "Co." (Irish
    county) -> ", co", "MD" (Madrid) -> ", md", or a hyphen-in-compound "Mexico - co-lo". Such a
    location leaks unless its country is in exclude_location_terms (several are listed there); a regex
    guard can't separate these from legit US "…, FL-Jacksonville"."""
    parts = []
    for t in terms:
        low = t.lower()
        if not low.strip():
            continue
        if re.fullmatch(r", [a-z]{2}", low):
            if allow_state_codes:
                parts.append(r"[,\-] " + low[2:] + r"\b")
        elif re.fullmatch(r"[a-z]{2,3}", low):
            parts.append(r"\b" + re.escape(low) + r"\b")
        else:
            parts.append(re.escape(low))
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def _matches_any(pattern: re.Pattern | None, *texts: str) -> bool:
    """True if `pattern` is set and hits any of `texts`. `_word_re` / `_region_matcher` both
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
        self._include_location_re = _region_matcher(include_location_terms)
        # For a bare "Remote" the country is absent from `location` and only in the TITLE
        # ("… (USA)" vs "… - Egypt Based"); reuse the same tokens minus the 2-letter state codes
        # (", or"/", in" would hit the words "or"/"in" in a title).
        self._include_title_re = _region_matcher(include_location_terms, allow_state_codes=False)
        # Whole-word backstop for the include allowlist: "india" drops ", India" but not
        # "Indiana"/"Indianapolis" (safe — location is a structured place-name, not free text).
        # Its remaining job (now "remote" is not an include token): drop a MIXED location that
        # cleared the allowlist on a US token but also names an excluded country ("Remote US & Canada").
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
        self._checks = (self._dept_allowed, self._location_not_excluded, self._region_allowed,
                        self._role_allowed, self._role_words_allowed, self._description_allowed)

    def keep(self, job: Job) -> bool:
        return all(check(job) for check in self._checks)

    def _dept_allowed(self, job: Job) -> bool:
        dept = job.department.lower()
        return not any(term in dept for term in self._exclude_dept_terms)

    def _location_not_excluded(self, job: Job) -> bool:
        # Backstop: drop a location that cleared the include allowlist but names an excluded
        # country (e.g. a mixed "Remote US & Canada").
        return not _matches_any(self._exclude_location_re, job.location)

    def _region_allowed(self, job: Job) -> bool:
        # The region signal usually lives in `location`; for a bare "Remote" it is only in the TITLE
        # instead (same multi-field pattern as _role_allowed). A concrete foreign location ("Remote
        # - Spain") is decided by location alone, never rescued by a stray token in its title.
        if not job.location:
            return False
        if _matches_any(self._include_location_re, job.location):
            return True
        if self._is_bare_remote(job.location):
            return _matches_any(self._include_title_re, _normalize_prose(job.title))
        return False

    @staticmethod
    def _is_bare_remote(location: str) -> bool:
        # "Remote" (or "Remote - Remote") with no other place named.
        return set(re.split(r"[\s,\-/;()]+", location.lower())).issubset({"remote", ""})

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
