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

_SCHEMA = {
    "type": "object",
    "properties": {
        "experience_score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["experience_score", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You rate a single job posting against the candidate resume below and return JSON.\n"
    "experience_score (0-100, integer): how well the candidate fits THIS specific role "
    "on skills, domain, and seniority. A role the candidate could not credibly apply to "
    "(non-engineering, wrong field, far too senior) scores near 0.\n\n"
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


def _parse_score(raw: str, scale: ScoreScale) -> Score:
    match = _JSON_RE.search(raw)
    if not match:
        raise ValueError(f"no JSON object found in scorer output; raw: {raw[:200]!r}")
    data = json.loads(match.group(0))
    if "experience_score" not in data:
        raise ValueError(f"scorer output missing experience_score; raw: {raw[:200]!r}")
    return Score(
        experience_score=_clamp(data["experience_score"]),
        reason=str(data.get("reason", "")).strip(),
        scale=scale,
    )


class _LlmScorer(ABC):
    """Template Method: shared prompt-building and response parsing; subclass implements `_invoke`."""

    scale = ScoreScale.LLM

    def __init__(self, resume_text: str, max_description_chars: int):
        self._resume = resume_text
        self._max_description_chars = max_description_chars

    def score(self, job: Job) -> Score:
        system = _SYSTEM.format(resume=self._resume)
        return _parse_score(self._invoke(system, self._job_blob(job)), self.scale)

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
                "json_schema": {"name": "job_scores", "strict": True, "schema": _SCHEMA},
            },
        }
        # reasoning_effort is only valid for reasoning models; omitting it for standard models.
        if self._reasoning_effort:
            request["reasoning_effort"] = self._reasoning_effort
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = self._client_instance().chat.completions.create(**request)
                return resp.choices[0].message.content
            except Exception as exc:
                last_error = exc
                log.warning("OpenAI scoring attempt %d failed: %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"OpenAI scoring failed after {self._max_retries} attempts") from last_error


class CliScorer(_LlmScorer):
    """Drives a local GPT CLI for users without an API key.
    Best-effort: output format is not guaranteed; JSON is extracted leniently."""

    method_label = "CLI"

    def __init__(self, command: list[str], resume_text: str, max_description_chars: int, timeout: int = 180):
        super().__init__(resume_text, max_description_chars)
        self._command = command  # full invocation including subcommand, e.g. ["codex", "exec"]
        self._timeout = timeout

    def _invoke(self, system_prompt: str, user_prompt: str) -> str:
        prompt = (
            f"{system_prompt}\n\n{user_prompt}\n\n"
            'Return ONLY a JSON object: {"experience_score": <int 0-100>, '
            '"reason": "<one sentence>"}. No other text.'
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
    pass_score = max((t.keyword_threshold for t in settings.tracks), default=50) + 1
    policy = settings.description_policy
    keyword_scorer = KeywordScorer(settings.skill_keywords,
                                   title_keywords=settings.scored_title_terms,
                                   description_policy=policy)
    return keyword_scorer, TitleOnlyAutoPass(keyword_scorer, pass_score, policy)
