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

from .models import Job, Score
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


def _parse_score(raw: str) -> Score:
    match = _JSON_RE.search(raw)
    if not match:
        raise ValueError(f"no JSON object found in scorer output; raw: {raw[:200]!r}")
    data = json.loads(match.group(0))
    if "experience_score" not in data:
        raise ValueError(f"scorer output missing experience_score; raw: {raw[:200]!r}")
    return Score(
        experience_score=_clamp(data["experience_score"]),
        reason=str(data.get("reason", "")).strip(),
    )


class _LlmScorer(ABC):
    """Template Method: shared prompt-building and response parsing; subclass implements `_invoke`."""

    def __init__(self, resume_text: str, max_description_chars: int):
        self._resume = resume_text
        self._max_description_chars = max_description_chars

    def score(self, job: Job) -> Score:
        system = _SYSTEM.format(resume=self._resume)
        return _parse_score(self._invoke(system, self._job_blob(job)))

    def _job_blob(self, job: Job) -> str:
        return (
            f"TITLE: {job.title}\n"
            f"COMPANY: {job.company}\n"
            f"LOCATION: {job.location}\n"
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
    nor GPT CLI is available."""

    method_label = "Keyword"

    _WORD_RE = re.compile(r"[a-z][a-z0-9+#.]{4,}")

    def __init__(self, resume_text: str = ""):
        self._resume_tokens = set(self._WORD_RE.findall(resume_text.lower()))

    def score(self, job: Job) -> Score:
        if self._resume_tokens:
            text = f"{job.title} {job.description}".lower()
            overlap = len(self._resume_tokens & set(self._WORD_RE.findall(text)))
            # experience still gates the track threshold (>50 needs overlap >= 4); the
            # raw count rides along because the clamp saturates at overlap 20.
            return Score(_clamp(40 + 3 * overlap), "keyword-only heuristic", matches=overlap)
        # No resume: constant score puts every role on the same side of the
        # threshold, so matches stays None — there's no meaningful count to report.
        return Score(50, "keyword-only heuristic")


def build_scorer(settings) -> JobScorer:
    if settings.openai_api_key:
        log.info("scorer: OpenAI API (%s)", settings.model)
        return OpenAiScorer(
            settings.openai_api_key, settings.model, settings.resume_text,
            settings.max_description_chars, settings.reasoning_effort,
        )
    if settings.gpt_cli and shutil.which(settings.gpt_cli):
        command = [settings.gpt_cli, *settings.gpt_cli_args]
        log.info("scorer: GPT CLI '%s' (no API key found)", " ".join(command))
        return CliScorer(command, settings.resume_text, settings.max_description_chars)
    log.info("scorer: keyword-only fallback (no API key or GPT CLI found)")
    return KeywordScorer(settings.resume_text)
