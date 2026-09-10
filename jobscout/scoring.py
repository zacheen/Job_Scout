"""Resume-vs-role job scoring: three strategies tried in fidelity order.

OpenAI API -> local GPT CLI (e.g. Codex via ChatGPT login) -> keyword heuristic.
`build_scorer` selects the best available at startup.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import replace

from .models import DescriptionPolicy, Job, Score, ScoreScale
from .protocols import JobScorer

log = logging.getLogger(__name__)

_SYSTEM = (
    "You rate a single job posting against the candidate resume below and return JSON.\n"
    "experience_score (0-100, integer): how well the candidate fits THIS specific role "
    "on skills, domain, and seniority. A role the candidate could not credibly apply to "
    "(non-engineering, wrong field, far too senior) scores near 0.\n"
    "work_auth_barrier (boolean): true only when the DESCRIPTION itself states a "
    "requirement that bars a candidate who needs visa sponsorship — sponsorship refused "
    "or unavailable, US citizenship or permanent residence required, or a security "
    "clearance required. False when the description is empty or silent on it; do not "
    "infer a bar from the employer's industry. This must NOT move experience_score: "
    "rate fit as if the barrier were absent, so a strong-fit role the candidate cannot "
    "legally take still scores high and is merely flagged.\n\n"
    "CANDIDATE RESUME:\n{resume}"
)

# Greedy: captures outermost {...} so surrounding CLI chatter is ignored.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _clamp(value) -> int:
    return max(0, min(100, int(value)))


def _compile_patterns(keywords) -> dict[str, re.Pattern]:
    """keyword -> boundary-aware pattern. Custom lookarounds (not \\b) so "c++"/"c#"/"3d"
    still match; the trailing "s?" absorbs plurals ("api" hits "APIs") and, as a side
    effect, keeps "java" from bleeding into "javascript". The [a-z0-9] boundaries keep
    short tokens (go/ai/ml/rl) out of google/email/html/world."""
    return {kw: re.compile(rf"(?<![a-z0-9]){re.escape(kw)}s?(?![a-z0-9])")
            for kw in (k.strip().lower() for k in keywords) if kw}


def _hit_counts(patterns: dict[str, re.Pattern], text: str) -> dict[str, int]:
    """keyword -> occurrences in `text`, omitting keywords that never appear."""
    return {kw: n for kw, pat in patterns.items() if (n := len(pat.findall(text)))}


def _as_bool(value) -> bool:
    """Tolerant read of a JSON boolean: models answer `true`, `"true"` and `1` for the
    same thing. A missing key arrives as None and reads False, which is what keeps
    work_auth_barrier fail-open — see `_parse_score`."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _parse_score(raw: str, scale: ScoreScale) -> Score:
    match = _JSON_RE.search(raw)
    if not match:
        raise ValueError(f"no JSON object found in scorer output; raw: {raw[:200]!r}")
    data = json.loads(match.group(0))
    if "experience_score" not in data:
        raise ValueError(f"scorer output missing experience_score; raw: {raw[:200]!r}")
    if data.get("work_auth_barrier") is None:
        # Tests the VALUE, not key membership, so an explicit `"work_auth_barrier": null`
        # (a CLI model's way of saying it could not tell) logs like an omitted key rather
        # than passing for a real answer.
        # Logged for its ABSENCE, not its value: the fail-open default below makes a
        # scorer that stopped asking for the field look identical to a run where nothing
        # was flagged, which is how OpenAiScorer's schema once omitted it silently.
        # INFO, not DEBUG, because both entry points pin basicConfig to INFO with no
        # verbose switch — at DEBUG this line could never print, which is the same
        # silence it exists to break. Rare enough to not be noise: OpenAiScorer's schema
        # lists the key as required, so only a misbehaving CLI model reaches here.
        log.info("scorer response omitted work_auth_barrier; keys: %s", sorted(data))
    return Score(
        experience_score=_clamp(data["experience_score"]),
        reason=str(data.get("reason", "")).strip(),
        scale=scale,
        # Absent key defaults to False rather than raising, unlike experience_score: this
        # is a backstop behind PreFilter's term list, so a model that ignores the field
        # must cost one missing annotation, never the whole run's scoring.
        work_auth_barrier=_as_bool(data.get("work_auth_barrier")),
    )


class _LlmScorer(ABC):
    """Template Method: shared prompt-building and response parsing; subclass implements `_invoke`."""

    scale = ScoreScale.LLM

    def __init__(self, resume_text: str, max_description_chars: int):
        self._resume = resume_text
        self._max_description_chars = max_description_chars

    def score(self, job: Job) -> Score:
        system = _SYSTEM.format(resume=self._resume)
        scored = _parse_score(self._invoke(system, self._job_blob(job)), self.scale)
        if scored.work_auth_barrier:
            # Logged because reaching this means PreFilter's term list missed the wording:
            # the job is a candidate for a new exclude_description_terms row.
            log.info("work-auth barrier flagged by LLM, PreFilter did not: %s (%s)",
                     job.title, job.url)
        return scored

    def _job_blob(self, job: Job) -> str:
        return (
            f"TITLE: {job.title}\n"
            f"COMPANY: {job.company}\n"
            f"LOCATION: {job.display_location}\n"
            f"DESCRIPTION:\n{job.description[: self._max_description_chars]}"
        )

    @property
    @abstractmethod
    def method_label(self) -> str:
        """A plain class attribute (e.g. `method_label = "API"`) satisfies this abstract property."""

    @abstractmethod
    def _invoke(self, system_prompt: str, user_prompt: str) -> str:
        ...


class OpenAiScorer(_LlmScorer):
    """Client creation and secret validation are deferred to first `score()` call,
    so a seed-only first run never requires OPENAI_API_KEY or RESUME_TEXT."""

    method_label = "API"

    # HTTP statuses no amount of waiting fixes: malformed request, revoked or rotated key,
    # permission denied, unknown model. Retrying one burns 1+2+4s of sleep and three round
    # trips PER JOB on a run that cannot produce a digest either way. Class-level because
    # it describes THIS transport — CliScorer's subprocess failures carry no status_code.
    _FATAL_STATUS = frozenset({400, 401, 403, 404})

    # Private to this subclass, NOT a shared description of `_SYSTEM`'s contract: under
    # `strict: True` the model is grammar-constrained to this schema, so a key `_SYSTEM`
    # asks for but this omits is structurally impossible to return — and `_parse_score`
    # would read the silence as a legitimate answer. Every field added to `_SYSTEM` must
    # be added here too, in BOTH properties and required (strict mode rejects a schema
    # whose required list is not exhaustive).
    _SCHEMA = {
        "type": "object",
        "properties": {
            "experience_score": {"type": "integer"},
            "work_auth_barrier": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["experience_score", "work_auth_barrier", "reason"],
        "additionalProperties": False,
    }
    # Import-time guard for the half of the rule above that is mechanically checkable.
    # Adding a property and forgetting `required` otherwise surfaces as an OpenAI 400 on
    # the first scored job of a run, after the whole fetch stage has already been paid for.
    assert set(_SCHEMA["required"]) == set(_SCHEMA["properties"]), \
        "strict mode requires every property to be listed in required"

    def __init__(self, api_key: str, model: str, resume_text: str,
                 max_description_chars: int, reasoning_effort: str = "", max_retries: int = 3):
        super().__init__(resume_text, max_description_chars)
        self._api_key = api_key
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_retries = max_retries
        self._client = None

    def _validate_config(self) -> None:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        if not self._resume:
            raise RuntimeError("RESUME_TEXT is not set")

    def _client_instance(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def _invoke(self, system_prompt: str, user_prompt: str) -> str:
        self._validate_config()
        request = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "job_scores", "strict": True,
                                "schema": self._SCHEMA},
            },
        }
        # reasoning_effort is only valid for reasoning models; omitting it for standard models.
        if self._reasoning_effort:
            request["reasoning_effort"] = self._reasoning_effort
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self._max_retries):
            attempts = attempt + 1
            try:
                resp = self._client_instance().chat.completions.create(**request)
                return resp.choices[0].message.content
            except Exception as exc:
                last_error = exc
                log.warning("OpenAI scoring attempt %d failed: %s", attempts, exc)
                # status_code is read duck-typed so this file needs no openai import.
                if getattr(exc, "status_code", None) in self._FATAL_STATUS:
                    break
                if attempts == self._max_retries:
                    break  # nothing left to wait for; the sleep would only delay the raise
                time.sleep(2 ** attempt)
        raise RuntimeError(f"OpenAI scoring failed after {attempts} attempt(s)") from last_error


class CliScorer(_LlmScorer):
    """Drives a local GPT CLI for users without an API key.
    Best-effort: output format is not guaranteed; JSON is extracted leniently."""

    method_label = "CLI"

    def __init__(self, command: list[str], resume_text: str, max_description_chars: int, timeout: int = 180):
        super().__init__(resume_text, max_description_chars)
        self._command = command  # full invocation including subcommand, e.g. ["codex", "exec"]
        self._timeout = timeout

    def _invoke(self, system_prompt: str, user_prompt: str) -> str:
        # This key list is the CLI's substitute for OpenAiScorer's enforced schema, so it
        # has to name every field `system_prompt` asks for — a shorter list here reads as
        # "ignore the rest" and silently loses them.
        prompt = (
            f"{system_prompt}\n\n{user_prompt}\n\n"
            'Return ONLY a JSON object: {"experience_score": <int 0-100>, '
            '"work_auth_barrier": <true|false>, "reason": "<one sentence>"}. '
            "No other text."
        )
        result = subprocess.run(
            [*self._command, prompt],
            capture_output=True, text=True, timeout=self._timeout,
            # codex reads stdin regardless of the argv prompt; DEVNULL sends EOF immediately.
            # Without it, an inherited open-but-empty stdin (e.g. under a debugger/runner)
            # blocks forever.
            stdin=subprocess.DEVNULL,
            # CLI emits UTF-8; without this, text=True decodes via the OS locale
            # (cp950 on zh-TW Windows) and the reader thread dies on bytes like 0xe2.
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            # codex prefixes stderr with a ~200-char startup banner (version/workdir/model/...);
            # the head never has the actual error, hence taking the tail.
            raise RuntimeError(
                f"{' '.join(self._command)} exited {result.returncode}: {result.stderr[-300:]}"
            )
        return result.stdout


class KeywordScorer:
    """No-LLM fallback: low fidelity by design. Used only when neither API key
    nor GPT CLI is available.

    Counts occurrences of the configured `skill_keywords`, not every 5+ char word in
    the resume prose — the latter floods matches with filler ("strong", "experience",
    "global") and misses short skills like "c++"/"go"/"cnn"/"git".

    `title_keywords` are matched against the TITLE alone. Role nouns ("software",
    "engineer") are what a plain "Software Development Engineer" posting is made of, yet
    they are not skills, so without them such a title scores the 40 floor and is dropped
    — measured over 289 jobs the LLM also judged, the skills list alone caught 52% of the
    roles worth emailing, adding the title terms caught 86%. They must stay out of the
    description scan: a JD body repeats "engineer" whatever the role is, so scoring it
    there would lift every posting equally instead of separating them. The two kinds of
    hit reach `Score` separately (`match_counts` vs `title_match_counts`) so the email can
    show which is which — a title term says far less about fit than a matched skill does.

    Title-only listings (many list APIs omit the job-ad body — Eightfold, Workday, …)
    can never reach the ~4 distinct hits a description-backed role needs to clear a
    50-point keyword_threshold, so their hits weigh `_TITLE_ONLY_WEIGHT` instead of
    `_WEIGHT`. Groups that must never lose such a role skip the gate entirely — see
    TitleOnlyAutoPass. "Title-only" is DescriptionPolicy's call, not an emptiness test:
    a teaser body would otherwise claim the strict weight while carrying no requirements.
    """

    method_label = "Keyword"
    scale = ScoreScale.KEYWORD

    _BASE = 40
    _WEIGHT = 3             # per distinct keyword with a description (>50 needs >= 4)
    _TITLE_ONLY_WEIGHT = 8  # per distinct keyword in a bare title (>50 needs >= 2)

    def __init__(self, skill_keywords: list[str] = (), *,
                 description_policy: DescriptionPolicy, title_keywords: list[str] = ()):
        self._policy = description_policy
        # Both dicts are read-only after this point — score() relies on their keys staying
        # disjoint, which is enforced here and nowhere else.
        self._patterns = _compile_patterns(skill_keywords)
        # A term on both lists stays a description keyword: that scan covers the title
        # too, so it can only match more, and one term never counts under two rules.
        self._title_patterns = {kw: pat
                                for kw, pat in _compile_patterns(title_keywords).items()
                                if kw not in self._patterns}

    @staticmethod
    def _breakdown(counts: dict[str, int]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def score(self, job: Job) -> Score:
        title_only = not self._policy.is_usable(job.description)
        matches = match_counts = title_match_counts = None
        if self._patterns or self._title_patterns:
            skills = _hit_counts(self._patterns, f"{job.title} {job.description}".lower())
            titles = _hit_counts(self._title_patterns, job.title.lower())
            # experience scores the DISTINCT terms matched (the two sets of keys are
            # disjoint by construction); the breakdowns carry the per-keyword occurrence
            # counts the email shows.
            matches = len(skills) + len(titles)
            match_counts = self._breakdown(skills)
            title_match_counts = self._breakdown(titles)
        if matches is None:
            # No keywords configured at all: constant score puts every role on the same side
            # of the threshold, so matches stays None — no meaningful count to report.
            return Score(50, "keyword-only heuristic", scale=self.scale)
        weight = self._TITLE_ONLY_WEIGHT if title_only else self._WEIGHT
        reason = "keyword-only heuristic (title only)" if title_only else "keyword-only heuristic"
        return Score(_clamp(self._BASE + weight * matches), reason, scale=self.scale,
                     matches=matches, match_counts=match_counts,
                     title_match_counts=title_match_counts)


class TitleOnlyAutoPass:
    """Wraps a JobScorer and gives every DESCRIPTION-LESS posting a fixed passing score,
    instead of letting the gate judge text the source never provided. Wire it per group
    (see __main__) for the ones worth never missing.

    "Description-less" is DescriptionPolicy's call, so a source that answers with a teaser
    instead of the job ad cannot switch this off — the case it was written for is exactly
    the one an emptiness test misses.

    `inner` is still consulted for those postings, and only its score and reason are
    replaced: the keyword breakdown it returns is what the email prints and what orders
    the section, since every auto-passed role shares one experience_score. So only wrap a
    scorer whose score() is cheap — wrapping an LLM tier would buy a judgement this then
    throws away (build_scorer never does: it pairs this with the keyword fallback only)."""

    def __init__(self, inner: JobScorer, pass_score: int, description_policy: DescriptionPolicy):
        self._inner = inner
        self._pass_score = pass_score
        self._policy = description_policy

    @property
    def method_label(self) -> str:
        return self._inner.method_label

    @property
    def scale(self) -> ScoreScale:
        return self._inner.scale

    def score(self, job: Job) -> Score:
        scored = self._inner.score(job)
        if self._policy.is_usable(job.description):
            return scored
        return replace(scored, experience_score=_clamp(self._pass_score),
                       reason="title-only listing; auto-passed (no description to score)")


def build_scorer(settings) -> tuple[JobScorer, JobScorer | None]:
    """Second element: a lenient companion for groups that must never lose a title-only
    role — it auto-passes those (see `TitleOnlyAutoPass`). None for the LLM tiers, which
    can judge fit from a bare title themselves. Which groups get the companion is the
    caller's wiring decision (__main__), not decided here.
    """
    if settings.openai_api_key:
        log.info("scorer: OpenAI API (%s)", settings.model)
        return OpenAiScorer(
            settings.openai_api_key, settings.model, settings.resume_text,
            settings.max_description_chars, settings.reasoning_effort,
        ), None
    if settings.gpt_cli and shutil.which(settings.gpt_cli):
        command = [settings.gpt_cli, *settings.gpt_cli_args]
        log.info("scorer: GPT CLI '%s' (no API key found)", " ".join(command))
        return CliScorer(command, settings.resume_text, settings.max_description_chars), None
    log.info("scorer: keyword-only fallback (no API key or GPT CLI found)")
    # +1 over the highest keyword_threshold (this scorer's own gate), not one track's:
    # which track the job will route to isn't known here, so it must clear every track.
    # Read through threshold_for, not the raw field: that method is the one authority on
    # which column gates which ScoreScale, and pipeline's email gate reads it too.
    highest_threshold = max((t.threshold_for(ScoreScale.KEYWORD) for t in settings.tracks),
                            default=50)
    pass_score = highest_threshold + 1
    # TitleOnlyAutoPass runs pass_score through _clamp and the email gate is a strict `>`,
    # so a keyword_threshold of 100 would auto-pass at exactly 100, fail `100 > 100`, and
    # silently drop every title-only referral and intern role — the two groups the
    # auto-pass exists for. Loud here beats an unexplained empty digest.
    if _clamp(pass_score) <= highest_threshold:
        raise ValueError(
            f"keyword_threshold {highest_threshold} leaves the title-only auto-pass no "
            f"score above it (clamped to {_clamp(pass_score)}); lower the track thresholds "
            "in config.yaml")
    policy = settings.description_policy
    keyword_scorer = KeywordScorer(settings.skill_keywords,
                                   title_keywords=settings.scored_title_terms,
                                   description_policy=policy)
    return keyword_scorer, TitleOnlyAutoPass(keyword_scorer, pass_score, policy)
