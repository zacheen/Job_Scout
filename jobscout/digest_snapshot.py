"""Per-run digest snapshots: what a local run emailed, kept as JSON for results_gui.py.

The writer and the reader live together so the file format is known in one place.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Job, Score
from .protocols import Digest, Notifier

log = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
# Snapshots older than this are deleted, along with any todo they still held.
SNAPSHOT_KEEP_DAYS = 30
_PREFIX = "run-"
_STAMP = "%Y%m%dT%H%M%SZ"


def _job_record(group: str, track: str, job: Job, score: Score) -> dict:
    """One digest row in the same terms the email body prints it."""
    return {
        "id": job.job_uid,
        # The ats segment of the uid (AtsFetcher.uid_prefix), naming the feed that
        # found the role, such as simplify for a job-board row.
        "source": job.job_uid.split(":", 1)[0],
        "group": group,
        "track": track,
        "title": job.title,
        "company": job.company,
        "location": job.display_location,
        "department": job.department,
        "posted": job.display_posted,
        "url": job.url,
        "score": score.experience_score,
        "scale": str(score.scale),
        "matches": score.matches,
        "match_counts": [list(pair) for pair in score.match_counts or ()],
        "title_matches": [kw for kw, _ in score.title_match_counts or ()],
        "warnings": [w for w in (job.note, score.work_auth_caveat, job.season_caveat) if w],
        "reason": score.reason,
    }


def write_atomic(path: Path, payload: dict) -> None:
    """Replace `path` whole, so a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".snap.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stamp_of(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem[len(_PREFIX):], _STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def snapshot_paths(directory: Path) -> list[Path]:
    """Snapshot files, newest first. Names sort by time because the stamp is fixed-width."""
    return sorted(directory.glob(f"{_PREFIX}*.json"), reverse=True)


def load_snapshots(directory: Path) -> list[dict]:
    """Every readable snapshot, newest first. A file of another version or one that
    does not parse is skipped rather than failing the whole page."""
    runs = []
    for path in snapshot_paths(directory):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("version") == SNAPSHOT_VERSION:
            runs.append(data)
    return runs


class SnapshotNotifier:
    """Notifier decorator that sends through `inner`, then records what was sent.

    Recording follows a send that returned, so the page never lists a role the email
    did not carry. A failed write is logged and swallowed, because an exception here
    would reach Pipeline.run as an email failure and leave the ledger unsaved, which
    re-emails every role next run.
    """

    def __init__(self, inner: Notifier, directory: Path, footer: str = "",
                 keep_days: int = SNAPSHOT_KEEP_DAYS,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._inner = inner
        self._directory = directory
        self._footer = footer
        self._keep = timedelta(days=keep_days)
        self._clock = clock

    def send_digest(self, digest: Digest, subject: str | None = None) -> None:
        self._inner.send_digest(digest, subject=subject)
        # Exception, not OSError: a lone surrogate in a scraped title fails json.dump
        # with UnicodeEncodeError.
        try:
            now = self._record(digest, subject)
        except Exception:
            log.exception("digest snapshot not written, the results page will miss this run")
            return
        try:
            self._prune(now)
        except Exception:
            log.exception("old digest snapshots not pruned")

    def _record(self, digest: Digest, subject: str | None) -> datetime:
        now = self._clock()
        jobs = [_job_record(group, track, job, score)
                for group, sections in digest for track, items in sections
                for job, score in items]
        if jobs:
            self._directory.mkdir(exist_ok=True)
            write_atomic(self._directory / f"{_PREFIX}{now:{_STAMP}}.json", {
                "version": SNAPSHOT_VERSION,
                "run_at": now.isoformat(timespec="seconds"),
                "subject": subject or "",
                "footer": self._footer,
                "jobs": jobs,
            })
        return now

    def _prune(self, now: datetime) -> None:
        for path in snapshot_paths(self._directory):
            stamp = _stamp_of(path)
            if stamp is not None and now - stamp > self._keep:
                path.unlink(missing_ok=True)
