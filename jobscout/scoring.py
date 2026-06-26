"""LLM scoring via OpenAI structured outputs."""
from __future__ import annotations

import json
import logging
import time

from .models import Job, Score

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
    "2. experience_score: how well the candidate, described by the résumé below, "
    "fits THIS specific role on skills, domain, and seniority.\n"
    "Scores are integers from 0 to 100.\n\n"
    "CANDIDATE RÉSUMÉ:\n{resume}"
)


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


class Scorer:
    """Scores one job per call. Validates secrets and builds the client lazily,
    so a seed-only first run never needs OPENAI_API_KEY / RESUME_TEXT."""

    def __init__(self, api_key: str, model: str, resume_text: str,
                 max_description_chars: int, reasoning_effort: str = "",
                 max_retries: int = 3):
        self._api_key = api_key
        self._model = model
        self._resume = resume_text
        self._max_description_chars = max_description_chars
        self._reasoning_effort = reasoning_effort
        self._max_retries = max_retries
        self._client = None

    def _ensure_ready(self) -> None:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        if not self._resume:
            raise RuntimeError("RESUME_TEXT is not set")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key)

    def score(self, job: Job) -> Score:
        self._ensure_ready()
        messages = [
            {"role": "system", "content": _SYSTEM.format(resume=self._resume)},
            {"role": "user", "content": self._job_blob(job)},
        ]
        data = self._call(messages)
        return Score(
            computer_vision_score=_clamp(data["computer_vision_score"]),
            experience_score=_clamp(data["experience_score"]),
            reason=str(data.get("reason", "")).strip(),
        )

    def _job_blob(self, job: Job) -> str:
        description = job.description[: self._max_description_chars]
        return (
            f"TITLE: {job.title}\n"
            f"COMPANY: {job.company}\n"
            f"LOCATION: {job.location}\n"
            f"DESCRIPTION:\n{description}"
        )

    def _call(self, messages: list[dict]) -> dict:
        request = {
            "model": self._model,
            "messages": messages,
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
                resp = self._client.chat.completions.create(**request)
                return json.loads(resp.choices[0].message.content)
            except Exception as exc:  # transient API/network/parse errors
                last_error = exc
                log.warning("scoring attempt %d failed: %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"scoring failed after {self._max_retries} attempts") from last_error
