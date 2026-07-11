"""Immutable value objects passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, fields


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

    def __post_init__(self):
        # ATS JSON can carry explicit nulls (e.g. "department": null) that
        # item.get(key, "") won't catch since the key exists — None then
        # crashes downstream .lower()/regex. Coerce here once for every fetcher.
        for f in fields(self):
            if getattr(self, f.name) is None:
                object.__setattr__(self, f.name, "")


@dataclass(frozen=True)
class Score:
    experience_score: int   # 0-100, candidate-resume fit to this specific role
    reason: str
