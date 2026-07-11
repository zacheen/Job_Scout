"""CSV-backed ledger of seen jobs and their scores (the dedupe source of truth).

One row = one opening. The same opening fetched from several sources is merged into
a single row: rows are identified by job_key ("{company}:{ats_job_id}") OR by any
shared URL (compared via urls.canon_url), since aggregator sources assign their own
ids but link the same posting.

The ledger is a DIRECTORY of per-company CSV shards ("{company-slug}.csv") — small
enough for GUI diff viewers and easy to locate a company — but stays ONE table in
memory: every shard is absorbed on load, so cross-company URL dedup (same opening,
differently spelled company) keeps working.
"""
from __future__ import annotations

import csv
import logging
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from .models import Job, Score
from .urls import canon_url

log = logging.getLogger(__name__)

_FIELDS = [
    "job_key", "company", "title", "location", "department", "urls", "date_posted",
    "first_seen", "track", "scored", "score_method",
    "experience_score", "reason", "emailed", "source_uids",
]

# Fields describing the posting itself; on merge the newer snapshot wins.
_CONTENT_FIELDS = ("company", "title", "location", "department", "date_posted")
# Fields written together by one scoring pass; on merge they move as a block.
_SCORE_FIELDS = ("scored", "score_method", "experience_score", "reason")

# Merge fidelity order (matches build_scorer's preference). "" = a legacy row scored
# before score_method existed: real LLM output of unknown origin, so it only loses
# to rows whose method IS known.
_METHOD_RANK = {"API": 0, "CLI": 1, "Keyword": 2, "": 3}
_UNSCORED_RANK = 99


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid_suffix(job_uid: str) -> str:
    """The ATS job id inside "{ats}:{company}:{id}". maxsplit keeps ids that contain
    ':' intact (SpeedyApply uses the full URL as the id)."""
    parts = job_uid.split(":", 2)
    return parts[2] if len(parts) == 3 else job_uid


def _job_key(company: str, ats_job_id: str) -> str:
    # company comes from the posting (the real employer), NOT the uid's middle segment,
    # which for aggregator sources is the aggregator's name.
    return f"{company.strip()}:{ats_job_id}"


# Multi-value cell separator. NOT a space: source uids embed company names, which
# contain spaces ("google:Google Taiwan:123"). '|' never occurs in uids or real URLs.
_MULTI_SEP = "|"


def _split_multi(value: str) -> list[str]:
    return value.split(_MULTI_SEP) if value else []


def _join_multi(values: list[str]) -> str:
    return _MULTI_SEP.join(values)


def _score_rank(row: dict) -> int:
    if row.get("scored") != "true":
        return _UNSCORED_RANK
    return _METHOD_RANK.get(row.get("score_method", ""), _METHOD_RANK[""])


def row_sort_key(row: dict) -> tuple[str, str, str, str]:
    """Canonical CSV row order: company, title, then first_seen (ISO, so plain string
    sort == chronological). job_key tie-breaks identical triples — without it two
    same-titled same-day rows would swap freely between saves, producing fake diffs."""
    return (row.get("company", "").casefold(), row.get("title", "").casefold(),
            row.get("first_seen", ""), row.get("job_key", ""))


# Windows-reserved device names can't be created as files there; suffix them.
_RESERVED_SLUGS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)


def _company_slug(company: str) -> str:
    """Shard filename for a company: lowercase alnum runs joined by '-', so name
    variants that differ only in case/punctuation share a file, and the result is
    safe on both Windows (case-insensitive FS) and the Linux runner."""
    slug = re.sub(r"[^a-z0-9]+", "-", company.casefold()).strip("-")[:80] or "unknown"
    return f"{slug}-co" if slug in _RESERVED_SLUGS else slug


class CsvStore:
    """In-memory rows persisted to a directory of per-company CSV shards, indexed three
    ways: by job_key and by canonical URL to detect the same opening across sources,
    and by original source uid so the pipeline can keep addressing rows with Job.job_uid.

    NOTE: an existing shard (or absorbed legacy file) is the 'seeded' signal. First run
    creates the shards and records the current backlog without scoring or emailing.

    `legacy_files` = pre-split single-file ledgers: absorbed on load and deleted by
    the next save() so old checkouts self-migrate. A legacy-SCHEMA file (job_uid
    column) is likewise migrated in memory; save() always writes the current schema.
    """

    def __init__(self, dir_path: Path, track_priority: Sequence[str] = (),
                 legacy_files: Sequence[Path] = ()):
        self._dir = dir_path
        # Lower index = higher priority on track conflicts (config.yaml tracks order).
        self._track_rank = {name: i for i, name in enumerate(track_priority)}
        self._rows: list[dict] = []
        self._by_key: dict[str, dict] = {}
        self._by_url: dict[str, dict] = {}
        self._by_uid: dict[str, dict] = {}
        shards = sorted(dir_path.glob("*.csv")) if dir_path.is_dir() else []
        for shard in shards:
            self.absorb(shard)
        self._legacy = [f for f in legacy_files if f.exists()]
        for legacy in self._legacy:
            self.absorb(legacy)
        self._seeded = bool(shards) or bool(self._legacy)

    # ---- queries -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._rows)

    def is_seeded(self) -> bool:
        return self._seeded

    def known_uids(self) -> set[str]:
        """All original source uids ("{ats}:{company}:{id}"), matching the format
        fetchers use for their early-stop / seed checks."""
        return set(self._by_uid)

    def known_urls(self) -> set[str]:
        # Cross-source email dedup: same role from two sources shares a URL but has
        # different uids. Keys are CANONICAL (urls.canon_url) — callers must canonicalize
        # before membership tests. Empty URLs are never indexed, so distinct roles can't merge.
        return set(self._by_url)

    def exists(self, job: Job) -> dict | None:
        """A COPY of the row this job belongs to (same job_key or URL), or None —
        copying prevents callers from mutating ledger state behind the indexes."""
        row = self._find(_job_key(job.company, _uid_suffix(job.job_uid)),
                         [job.url] if job.url else [])
        return dict(row) if row is not None else None

    # ---- mutations -----------------------------------------------------------

    def add_seen(self, job: Job) -> None:
        self._insert(self._row_from_job(job))

    def set_score(self, job_uid: str, track: str, score: Score, method: str = "") -> None:
        if job_uid not in self._by_uid:
            raise KeyError(f"set_score for unknown uid {job_uid!r}; call add_seen first")
        row = self._by_uid[job_uid]
        row["track"] = track
        row["scored"] = "true"
        row["score_method"] = method
        row["experience_score"] = str(score.experience_score)
        row["reason"] = score.reason

    def mark_emailed(self, job_uids: list[str]) -> None:
        for uid in job_uids:
            self._by_uid[uid]["emailed"] = "true"

    def absorb(self, path: Path) -> None:
        """Merge every row of another ledger CSV (legacy or current schema) into this
        store; also how the store loads its own file."""
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            legacy = "job_uid" in (reader.fieldnames or [])
            for raw in reader:
                self._insert(self._from_legacy(raw) if legacy else self._normalize(raw))

    def merge_rows(self, existing: dict, incoming: dict) -> dict:
        """Fold `incoming` into `existing` (same opening seen again) and return it.
        existing's job_key stays: identity never changes after creation. Re-indexes
        itself, so merged-in urls/uids are immediately findable."""
        newer = incoming if incoming["first_seen"] >= existing["first_seen"] else existing
        for field in _CONTENT_FIELDS:
            existing[field] = newer[field]

        seen_dates = [d for d in (existing["first_seen"], incoming["first_seen"]) if d]
        existing["first_seen"] = min(seen_dates) if seen_dates else ""

        old_urls = _split_multi(existing["urls"])
        new_urls = [u for u in _split_multi(incoming["urls"]) if u not in old_urls]
        existing["urls"] = _join_multi(new_urls + old_urls)  # newest first

        uids = _split_multi(existing["source_uids"])
        uids += [u for u in _split_multi(incoming["source_uids"]) if u not in uids]
        existing["source_uids"] = _join_multi(uids)

        if _score_rank(incoming) < _score_rank(existing):
            for field in _SCORE_FIELDS:
                existing[field] = incoming[field]

        existing["track"] = self._merge_track(existing, incoming)

        if incoming["emailed"] == "true":
            existing["emailed"] = "true"  # never re-email an opening
        self._index(existing)
        return existing

    def save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        by_slug: dict[str, list[dict]] = {}
        for row in self._rows:
            by_slug.setdefault(_company_slug(row["company"]), []).append(row)
        for slug, rows in by_slug.items():
            rows.sort(key=row_sort_key)
            with (self._dir / f"{slug}.csv").open("w", newline="", encoding="utf-8") as fh:
                # lineterminator: csv defaults to CRLF; LF keeps local (Windows) and cloud
                # (Linux runner) commits byte-identical, avoiding whole-file EOL diffs.
                writer = csv.DictWriter(fh, fieldnames=_FIELDS, extrasaction="ignore",
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
        # A merge can re-attribute rows to another company (content fields: newest wins),
        # emptying a shard. Delete it, or the next load resurrects the stale rows.
        for stale in self._dir.glob("*.csv"):
            if stale.stem not in by_slug:
                log.warning("deleting orphan shard %s (no rows reference it after save)", stale)
                stale.unlink()
        # Shards now hold everything the legacy files did; a leftover just gets
        # re-absorbed on the next load (an idempotent union), so deletion is
        # best-effort — a GUI git client holding the file open (Windows sharing
        # violation) must not fail the whole save.
        remaining = []
        for legacy in self._legacy:
            try:
                legacy.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("could not delete legacy ledger %s (%s); retrying next save",
                            legacy, exc)
                remaining.append(legacy)
        self._legacy = remaining

    # ---- internals -----------------------------------------------------------

    def _find(self, job_key: str, urls: list[str]) -> dict | None:
        row = self._by_key.get(job_key.casefold())
        if row is None:
            for url in urls:
                match = self._by_url.get(canon_url(url))
                if match is not None:
                    return match
        return row

    def _insert(self, row: dict) -> None:
        urls = _split_multi(row["urls"])
        existing = self._find(row["job_key"], urls)
        if existing is None:
            self._rows.append(row)
            self._index(row)
            return
        # Triangle case: key matched `existing` while a URL points at another row.
        # Not auto-merged (a bad/generic URL could chain-merge distinct roles) — warn
        # so it can be resolved by hand; never observed in real data so far.
        for url in urls:
            other = self._by_url.get(canon_url(url))
            if other is not None and other is not existing:
                log.warning("row %r also matches %r via url %s; merged into the former only",
                            existing["job_key"], other["job_key"], url)
        self.merge_rows(existing, row)

    def _index(self, row: dict) -> None:
        self._by_key[row["job_key"].casefold()] = row
        for url in _split_multi(row["urls"]):
            # Indexed under the canonical form; the row keeps the original strings.
            self._by_url[canon_url(url)] = row
        for uid in _split_multi(row["source_uids"]):
            self._by_uid[uid] = row

    def _merge_track(self, existing: dict, incoming: dict) -> str:
        old, new = existing["track"], incoming["track"]
        if not old or not new or old == new:
            return old or new
        default = len(self._track_rank)  # unknown track names lose to configured ones
        keep = min(old, new, key=lambda t: self._track_rank.get(t, default))
        # ASCII-only message: non-ASCII garbles on a cp950 (zh-TW Windows) console.
        log.warning("track conflict for %s: %r vs %r; keeping %r",
                    existing["job_key"], old, new, keep)
        return keep

    @staticmethod
    def _row_from_job(job: Job) -> dict:
        company = job.company.strip()
        return {
            "job_key": _job_key(company, _uid_suffix(job.job_uid)),
            "company": company,
            "title": job.title,
            "location": job.location,
            "department": job.department,
            "urls": job.url,
            "date_posted": job.date_posted,
            "first_seen": _now(),
            "track": "",
            "scored": "false",
            "score_method": "",
            "experience_score": "",
            "reason": "",
            "emailed": "false",
            "source_uids": job.job_uid,
        }

    @staticmethod
    def _from_legacy(old: dict) -> dict:
        uid = old["job_uid"]
        company = (old.get("company") or "").strip()
        return {
            "job_key": _job_key(company, _uid_suffix(uid)),
            "company": company,
            "title": old.get("title", ""),
            "location": old.get("location", ""),
            "department": old.get("department", ""),
            "urls": (old.get("url") or "").strip(),
            "date_posted": old.get("date_posted", ""),
            "first_seen": old.get("first_seen", ""),
            "track": old.get("track", ""),
            "scored": old.get("scored", "false"),
            "score_method": "",  # scored before score_method existed: origin unknown
            "experience_score": old.get("experience_score", ""),
            "reason": old.get("reason", ""),
            "emailed": old.get("emailed", "false"),
            "source_uids": uid,
        }

    @staticmethod
    def _normalize(raw: dict) -> dict:
        return {field: (raw.get(field) or "") for field in _FIELDS}


def union_merge(primary_dir: Path, track_priority: Sequence[str] = (), *,
                extra_dirs: Sequence[Path] = (), extra_files: Sequence[Path] = (),
                legacy_files: Sequence[Path] = ()) -> CsvStore:
    """Fold other ledgers into `primary_dir` and save the union back to it: every
    shard of each extra_dir, each extra csv file, and any still-existing legacy
    single-file ledger (deleted after the save). The ONE union-merge used by both
    local_run.py and scan.yml's push-race retry — keep them behaviorally identical."""
    store = CsvStore(primary_dir, track_priority, legacy_files=legacy_files)
    for extra_dir in extra_dirs:
        for shard in sorted(extra_dir.glob("*.csv")):
            store.absorb(shard)
    for path in extra_files:
        store.absorb(path)
    store.save()
    return store
