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


@dataclass(frozen=True)
class Score:
    relevance_score: int    # 0-100, fit to the job's assigned track
    experience_score: int   # 0-100, fit to the candidate's resume
    reason: str
