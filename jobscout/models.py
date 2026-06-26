"""Immutable value objects passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    # Format: "{ats}:{company}:{ats_job_id}" — used as the dedupe key across all stores.
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
    computer_vision_score: int  # 0-100, how much the role is computer-vision
    experience_score: int       # 0-100, fit to the candidate's résumé
    reason: str
