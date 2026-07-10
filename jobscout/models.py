"""Immutable value objects passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    # "{ats}:{company}:{ats_job_id}" — dedupe key across all stores.
    job_uid: str
    company: str
    title: str
    location: str
    url: str
    description: str
    department: str = ""
    date_posted: str = ""
    # Pipeline-derived caveat shown in the email (e.g. "possibly no visa sponsorship");
    # never persisted (CsvStore's fixed _FIELDS ignore it).
    note: str = ""


@dataclass(frozen=True)
class Score:
    experience_score: int   # 0-100, candidate-resume fit to this specific role
    reason: str
