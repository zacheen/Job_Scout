"""Load YAML settings and company list; read secrets from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Company:
    name: str
    ats: str
    params: dict[str, str]  # ATS-specific keys (board / org / host / tenant / site)


@dataclass(frozen=True)
class Settings:
    companies: list[Company]
    keywords: list[str]
    location_us_terms: list[str]
    cv_threshold: int
    model: str
    reasoning_effort: str
    max_description_chars: int
    request_timeout: int
    user_agent: str
    # Secrets — empty when unset; the consuming component validates on use.
    openai_api_key: str
    resume_text: str
    gmail_user: str
    gmail_app_password: str
    mail_to: str

    @classmethod
    def load(cls, root: Path) -> "Settings":
        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        companies_raw = yaml.safe_load((root / "companies.yaml").read_text(encoding="utf-8"))
        return cls(
            companies=[cls._to_company(d) for d in companies_raw],
            keywords=cfg["prefilter_keywords"],
            location_us_terms=cfg["location_us_terms"],
            cv_threshold=int(cfg.get("cv_threshold", 50)),
            model=cfg.get("model", "gpt-5.5"),
            reasoning_effort=cfg.get("reasoning_effort", ""),
            max_description_chars=int(cfg.get("max_description_chars", 8000)),
            request_timeout=int(cfg.get("request_timeout", 20)),
            user_agent=cfg.get("user_agent", "job-scout/1.0"),
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
        return Company(name=name, ats=ats, params={k: str(v) for k, v in entry.items()})

    @staticmethod
    def _read_resume(root: Path) -> str:
        # Local-dev fallback when RESUME_TEXT is unset: read a gitignored resume.txt.
        path = root / "resume.txt"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""
