"""Load YAML settings, companies, and tracks; read secrets/overrides from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


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


@dataclass(frozen=True)
class Track:
    name: str              # shown in the email subject, e.g. "Computer Vision"
    description: str       # fed to the LLM as the target this track scores against
    keywords: list[str]    # route a job to this track + score it on the keyword tier
    threshold: int         # minimum relevance_score to email
    # Title override: after keyword routing picks a track, a whole-word match of any
    # of these in the TITLE reclassifies the job into THIS track (e.g. "Senior").
    title_terms: list[str]

    def is_keyword_track(self) -> bool:
        """Participates in base keyword routing (an override-only track does not)."""
        return bool(self.keywords)

    def score_terms(self) -> list[str]:
        """Terms for the no-LLM keyword-fallback scorer. An override-only track falls
        back to its title_terms — with empty keywords it would be stuck at the floor
        score and silently never pass its threshold."""
        return self.keywords or self.title_terms


@dataclass(frozen=True)
class Settings:
    companies: list[Company]
    tracks: list[Track]
    exclude_terms: list[str]
    exclude_dept_terms: list[str]
    exclude_word_terms: list[str]
    exclude_description_terms: list[str]
    warn_description_terms: list[str]
    intern_terms: list[str]
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
    ledger_path: str
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
            warn_description_terms=cfg.get("warn_description_terms", []),
            intern_terms=cfg.get("intern_terms", ["intern", "internship", "co-op", "coop"]),
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
            # LEDGER_FILE overrides config so local and cloud runs use separate ledgers.
            ledger_path=os.getenv("LEDGER_FILE") or cfg.get("ledger_path", "data/seen_jobs.csv"),
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
        track = Track(
            name=entry["name"],
            description=entry.get("description", entry["name"]),
            # keywords optional: an override-only track (e.g. "Senior") has just title_terms.
            keywords=[k.lower() for k in entry.get("keywords", [])],
            threshold=int(entry.get("threshold", 50)),
            title_terms=[t.lower() for t in entry.get("title_terms", [])],
        )
        if not track.keywords and not track.title_terms:
            # Fail fast: with neither, the track is dead config (listed but unroutable), e.g. a keywords: typo.
            raise ValueError(f"track {track.name!r} has neither keywords nor title_terms")
        return track

    @staticmethod
    def _read_resume(root: Path) -> str:
        # Fallback when RESUME_TEXT env var is unset: read a gitignored resume.txt.
        path = root / "resume.txt"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""
