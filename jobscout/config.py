"""Load YAML settings, companies, and tracks; read secrets/overrides from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .models import ScoreScale

# tz shared by every digest timestamp (email subject line, local_run.py's
# footer) so footer times stay directly comparable to subject times across runs.
DIGEST_TZ = ZoneInfo("America/New_York")

# Untracked, gitignored stamp at the repo root: the last moment local_data caught
# up with cloud-emailed roles. Advanced by local_run.py (scan start, on success)
# and merge_seen_jobs.py --to local (fold time); local_run.py's digest footer
# reads it as the deletable window's lower bound.
DIGEST_CHECKPOINT_FILENAME = "digest_checkpoint.txt"

# Untracked, gitignored append-only record at the repo root, written by everything that
# knows a run saw less than it should (coverage.catchup_log): one line per company whose
# pull hit fetchers._MAX_JOBS_PER_RUN and so left older roles unfetched, plus any
# date_posted shape dates.posted_iso could not read. Survives the post-scan reset --hard
# because it is untracked, so it accumulates across runs — read it to decide whether the
# cap needs raising, or whether a company needs a persistent coverage checkpoint instead.
CATCHUP_LOG_FILENAME = "catchup_cap_hits.txt"


def _as_bool(value) -> bool:
    """Parse a YAML scalar as bool: native booleans pass through, strings use truthy words."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class Company:
    name: str
    ats: str
    params: dict[str, str]  # ATS-specific keys (board / org / host / tenant / site)
    seed_only: bool = False  # pipeline flag: first appearance seeds silently, then only new roles email

    def param_bool(self, key: str, default: bool = False) -> bool:
        """Boolean ATS param (params values are str() coerced, so "True"/"true"/"1" all work)."""
        value = self.params.get(key)
        return _as_bool(value) if value is not None else default


@dataclass(frozen=True)
class Track:
    name: str              # shown in the email section header, e.g. "Computer Vision"
    keywords: list[str]    # routing terms, matched as lowercase substrings in title/description
    # One gate per ScoreScale — the scorers' outputs aren't comparable, so neither
    # value is a safe default for the other.
    llm_threshold: int
    keyword_threshold: int
    min_hits: int = 1      # keyword hits (title + description, repeats count) needed to route here
    # Whole-word, TITLE-only terms for tokens unsafe as substrings ("ai" hits "email")
    # or too common in JD bodies to scan descriptions with ("engineer"); hits add to min_hits.
    word_keywords: list[str] = field(default_factory=list)

    def threshold_for(self, scale: ScoreScale) -> int:
        """The gate a score on `scale` must EXCEED to be emailed.

        Exhaustive on purpose: a new ScoreScale must fail here rather than silently
        inherit the LLM gate and quietly change which roles get emailed."""
        if scale is ScoreScale.LLM:
            return self.llm_threshold
        if scale is ScoreScale.KEYWORD:
            return self.keyword_threshold
        raise ValueError(f"track {self.name!r} has no threshold for scale {scale!r}")


@dataclass(frozen=True)
class Settings:
    companies: list[Company]
    tracks: list[Track]
    exclude_terms: list[str]
    exclude_dept_terms: list[str]
    exclude_word_terms: list[str]
    exclude_description_terms: list[str]
    exclude_description_patterns: list[str]
    exempt_role_phrases: list[str]
    warn_description_terms: list[str]
    skill_keywords: list[str]
    title_keywords: list[str]
    intern_terms: list[str]
    senior_terms: list[str]
    referral_companies: list[str]
    include_location_terms: list[str]
    exclude_location_terms: list[str]
    model: str
    reasoning_effort: str
    gpt_cli: str
    gpt_cli_args: list[str]
    max_description_chars: int
    score_workers: int
    request_timeout: int
    user_agent: str
    request_delay_min: float
    request_delay_max: float
    ledger_dir: str
    # Secrets may be empty strings; each consuming component validates on first use.
    openai_api_key: str
    resume_text: str
    gmail_user: str
    gmail_app_password: str
    mail_to: str

    @property
    def track_names(self) -> list[str]:
        """Track names in config order — also CsvStore's track-conflict merge priority."""
        return [t.name for t in self.tracks]

    @classmethod
    def load(cls, root: Path) -> "Settings":
        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        companies_raw = yaml.safe_load((root / "companies.yaml").read_text(encoding="utf-8"))
        return cls(
            companies=[cls._to_company(d) for d in companies_raw],
            tracks=[cls._to_track(d) for d in cfg["tracks"]],
            exclude_terms=cfg.get("exclude_terms", []),
            exclude_dept_terms=cfg.get("exclude_dept_terms", []),
            exclude_word_terms=cfg.get("exclude_word_terms", []),
            exclude_description_terms=cfg.get("exclude_description_terms", []),
            exclude_description_patterns=cfg.get("exclude_description_patterns", []),
            exempt_role_phrases=cfg.get("exempt_role_phrases", []),
            warn_description_terms=cfg.get("warn_description_terms", []),
            skill_keywords=[k.lower() for k in cfg.get("skill_keywords", [])],
            title_keywords=[k.lower() for k in cfg.get("title_keywords", [])],
            intern_terms=cfg.get("intern_terms", ["intern", "internship", "co-op", "coop"]),
            senior_terms=cfg.get("senior_terms", []),
            referral_companies=cfg.get("referral_companies", []),
            include_location_terms=cfg["include_location_terms"],
            exclude_location_terms=cfg.get("exclude_location_terms", []),
            model=cfg.get("model", "gpt-5.5"),
            reasoning_effort=cfg.get("reasoning_effort", ""),
            gpt_cli=os.getenv("GPT_CLI") or cfg.get("gpt_cli", "codex"),
            gpt_cli_args=cfg.get("gpt_cli_args", ["exec"]),
            max_description_chars=int(cfg.get("max_description_chars", 8000)),
            score_workers=int(cfg.get("score_workers", 5)),
            request_timeout=int(cfg.get("request_timeout", 20)),
            user_agent=cfg.get("user_agent", "job-scout/1.0"),
            request_delay_min=float(cfg.get("request_delay_min", 1.25)),
            request_delay_max=float(cfg.get("request_delay_max", 2.0)),
            # LEDGER_DIR overrides config so local and cloud runs use separate
            # ledgers (scan.yml points it into its data-branch checkout).
            ledger_dir=os.getenv("LEDGER_DIR") or cfg.get("ledger_dir", "local_data"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            resume_text=os.getenv("RESUME_TEXT", "") or cls._read_resume(root),
            gmail_user=os.getenv("GMAIL_USER", ""),
            gmail_app_password=os.getenv("GMAIL_APP_PASSWORD", ""),
            mail_to=os.getenv("MAIL_TO", ""),
        )

    @staticmethod
    def _to_company(entry: dict) -> Company:
        entry = dict(entry)
        name = entry.pop("name")
        ats = entry.pop("ats")
        seed_only = _as_bool(entry.pop("seed_only", False))
        return Company(name=name, ats=ats, seed_only=seed_only,
                       params={k: str(v) for k, v in entry.items()})

    @staticmethod
    def _to_track(entry: dict) -> Track:
        if "threshold" in entry:
            # Fail fast instead of silently applying the defaults below: the single
            # `threshold` key was split per ScoreScale, so a config still carrying it
            # (e.g. one merged in from `main`) would quietly gate on 50/50.
            raise ValueError(
                f"track {entry.get('name')!r} uses the removed 'threshold' key; "
                "replace it with llm_threshold and keyword_threshold")
        track = Track(
            name=entry["name"],
            keywords=[k.lower() for k in entry.get("keywords", [])],
            llm_threshold=int(entry.get("llm_threshold", 50)),
            keyword_threshold=int(entry.get("keyword_threshold", 50)),
            min_hits=int(entry.get("min_hits", 1)),
            word_keywords=[k.lower() for k in entry.get("word_keywords", [])],
        )
        if not track.keywords and not track.word_keywords:
            # Fail fast: a keyword-less track is dead config (listed but unroutable), e.g. a keywords: typo.
            raise ValueError(f"track {track.name!r} has no keywords")
        if track.min_hits < 1:
            raise ValueError(f"track {track.name!r} min_hits must be >= 1, got {track.min_hits}")
        return track

    @staticmethod
    def _read_resume(root: Path) -> str:
        # Fallback when RESUME_TEXT env var is unset: read a gitignored resume.txt.
        path = root / "resume.txt"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""
