"""Job scoring strategies and a selector.

Three implementations satisfy the JobScorer protocol, tried in order of fidelity:
OpenAI API -> a local GPT CLI (e.g. Codex, authed via a ChatGPT login) -> a
no-LLM keyword heuristic. `build_scorer` picks the best one available at startup.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Final

from .models import Job, Score
from .protocols import JobScorer

log = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "computer_vision_score": {"type": "integer"},
        "experience_score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["computer_vision_score", "experience_score", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You rate a single job posting on two independent 0-100 axes and return JSON.\n"
    "1. computer_vision_score: how strongly this role is about the field of "
    "computer-vision (image/video/visual perception, detection, segmentation, 3D "
    "vision, visual deep learning). A pure NLP, backend, or non-visual role scores "
    "low even when it is otherwise machine-learning.\n"
    "2. experience_score: how well the candidate, described by the resume below, "
    "fits THIS specific role on skills, domain, and seniority.\n"
    "Scores are integers from 0 to 100.\n\n"
    "CANDIDATE RESUME:\n{resume}"
)

# Greedy match grabs the outermost {...}, tolerating chatter around it (CLI output).
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_WORD_RE = re.compile(r"[a-z][a-z0-9+#.]{4,}")


def _clamp(value) -> int:
    return max(0, min(100, int(value)))


def _parse_scores(raw: str) -> Score:
    match = _JSON_RE.search(raw)
    if not match:
        raise ValueError(f"no JSON object found in scorer output; raw: {raw[:200]!r}")
    data = json.loads(match.group(0))
    missing = {"computer_vision_score", "experience_score"} - data.keys()
    if missing:
        raise ValueError(f"scorer output missing fields {missing}; raw: {raw[:200]!r}")
    return Score(
        computer_vision_score=_clamp(data["computer_vision_score"]),
        experience_score=_clamp(data["experience_score"]),
        reason=str(data.get("reason", "")).strip(),
    )


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


class _LlmScorer(ABC):
    """Template for LLM-backed scorers: shared prompt + parsing, subclass supplies
    the call that turns the prompt into a raw response string."""

    def __init__(self, resume_text: str, max_description_chars: int):
        self._resume = resume_text
        self._max_description_chars = max_description_chars

    def score(self, job: Job) -> Score:
        return _parse_scores(self._invoke(_SYSTEM.format(resume=self._resume), self._job_blob(job)))

    def _job_blob(self, job: Job) -> str:
        return (
            f"TITLE: {job.title}\n"
            f"COMPANY: {job.company}\n"
            f"LOCATION: {job.location}\n"
            f"DESCRIPTION:\n{job.description[: self._max_description_chars]}"
        )

    @abstractmethod
    def _invoke(self, system_prompt: str, user_prompt: str) -> str:
        ...


class OpenAiScorer(_LlmScorer):
    """Builds the client and validates secrets lazily, so a seed-only first run
    never needs OPENAI_API_KEY / RESUME_TEXT."""

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
        # reasoning_effort applies only to reasoning models (gpt-5.5); omit for others.
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
    """Drives a local GPT CLI (default `codex exec`) for users without an API key.
    Best-effort: the CLI's exact output format is not guaranteed, so the JSON is
    extracted leniently and a parse/exit failure surfaces to the caller."""

    def __init__(self, command: list[str], resume_text: str, max_description_chars: int, timeout: int = 180):
        super().__init__(resume_text, max_description_chars)
        self._command = command  # full invocation incl. subcommand, e.g. ["codex", "exec"]
        self._timeout = timeout

    def _invoke(self, system_prompt: str, user_prompt: str) -> str:
        prompt = (
            f"{system_prompt}\n\n{user_prompt}\n\n"
            'Return ONLY a JSON object: {"computer_vision_score": <int 0-100>, '
            '"experience_score": <int 0-100>, "reason": "<one sentence>"}. No other text.'
        )
        result = subprocess.run(
            [*self._command, prompt],
            capture_output=True, text=True, timeout=self._timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(self._command)} exited {result.returncode}: {result.stderr[:200]}"
            )
        return result.stdout


class KeywordScorer:
    """No-LLM fallback. computer_vision_score reflects CV-keyword strength;
    experience_score is resume/job word overlap. Low fidelity by design — only
    used when neither an API key nor a GPT CLI is available."""

    _STRONG_CV: Final = (
        "computer vision", "vision", "image", "video", "segmentation", "detection",
        "perception", "slam", "point cloud", "ocr", "visual", "3d",
    )

    def __init__(self, resume_text: str = ""):
        self._resume_tokens = _tokens(resume_text)

    def score(self, job: Job) -> Score:
        text = f"{job.title} {job.description}".lower()
        hits = sum(1 for kw in self._STRONG_CV if kw in text)
        cv = _clamp(45 + 12 * hits) if hits else 40
        if self._resume_tokens:
            overlap = len(self._resume_tokens & _tokens(text))
            exp = _clamp(40 + 3 * overlap)
        else:
            exp = 50
        return Score(cv, exp, "keyword-only heuristic (no LLM available)")


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
