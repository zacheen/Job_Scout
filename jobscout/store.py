"""CSV-backed ledger of seen jobs and their scores (the dedupe source of truth)."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .models import Job, Score

_FIELDS = [
    "job_uid", "company", "title", "location", "department", "url", "date_posted",
    "first_seen", "scored", "computer_vision_score", "experience_score", "reason", "emailed",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CsvStore:
    """In-memory rows keyed by job_uid, loaded from and saved to one CSV file.

    The file's prior existence is the 'seeded' signal: the very first run finds
    no file, records the current backlog, and skips scoring/email.
    """

    def __init__(self, path: Path):
        self._path = path
        self._rows: dict[str, dict] = {}
        self._seeded = path.exists()
        if self._seeded:
            self._load()

    def _load(self) -> None:
        with self._path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self._rows[row["job_uid"]] = row

    def is_seeded(self) -> bool:
        return self._seeded

    def known_uids(self) -> set[str]:
        return set(self._rows)

    def add_seen(self, job: Job) -> None:
        self._rows[job.job_uid] = {
            "job_uid": job.job_uid,
            "company": job.company,
            "title": job.title,
            "location": job.location,
            "department": job.department,
            "url": job.url,
            "date_posted": job.date_posted,
            "first_seen": _now(),
            "scored": "false",
            "computer_vision_score": "",
            "experience_score": "",
            "reason": "",
            "emailed": "false",
        }

    def set_score(self, job_uid: str, score: Score) -> None:
        if job_uid not in self._rows:
            raise KeyError(f"set_score for unknown uid {job_uid!r}; call add_seen first")
        row = self._rows[job_uid]
        row["scored"] = "true"
        row["computer_vision_score"] = str(score.computer_vision_score)
        row["experience_score"] = str(score.experience_score)
        row["reason"] = score.reason

    def mark_emailed(self, job_uids: list[str]) -> None:
        for uid in job_uids:
            self._rows[uid]["emailed"] = "true"

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FIELDS)
            writer.writeheader()
            writer.writerows(self._rows.values())
