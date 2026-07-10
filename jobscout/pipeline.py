"""Orchestrates a single scan run: fetch -> filter -> route -> score -> one digest email."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from collections.abc import Collection

from .config import Track
from .models import Job, Score
from .protocols import (Annotator, Digest, Fetcher, JobFilter, JobScorer, JobStore, Leveler,
                        Notifier, Router)

log = logging.getLogger(__name__)


class _ScoreAttempt(NamedTuple):
    job: Job
    track: Track
    score: Score | None  # None = scoring failed (already logged, not re-raised)


class Pipeline:
    def __init__(
        self,
        *,
        store: JobStore,
        fetcher: Fetcher,
        prefilter: JobFilter,
        annotator: Annotator,
        router: Router,
        leveler: Leveler,
        scorer: JobScorer,
        notifier: Notifier,
        score_workers: int = 1,
        seed_only_prefixes: Collection[str] = (),
    ):
        if score_workers < 1:
            raise ValueError(f"score_workers must be >= 1, got {score_workers}")
        self._store = store
        self._fetcher = fetcher
        self._prefilter = prefilter
        self._annotator = annotator
        self._router = router
        self._leveler = leveler
        self._scorer = scorer
        self._notifier = notifier
        self._score_workers = score_workers
        # uid prefixes of sources that seed silently on their first appearance (see run()).
        self._seed_only_prefixes = tuple(seed_only_prefixes)

    def run(self) -> None:
        all_jobs = self._fetch_all()
        # Snapshot uids + urls BEFORE add_seen, so this run's own jobs don't dedup themselves.
        known = self._store.known_uids()
        known_urls = self._store.known_urls()
        candidates = [j for j in all_jobs if self._prefilter.keep(j)]
        # Annotate only the genuinely new candidates — steady-state runs mostly refetch
        # already-known jobs. Annotated copies flow only to the email path; add_seen below
        # records the originals from all_jobs.
        new_candidates = [self._annotate(j) for j in candidates if j.job_uid not in known]
        new_candidates = self._suppress_seeding(new_candidates, known)
        # Record all new fetched jobs, not only candidates: PreFilter-rejected jobs are
        # otherwise never recorded and look "new" forever, defeating early-stop.
        new_fetched = [j for j in all_jobs if j.job_uid not in known]
        for job in new_fetched:
            self._store.add_seen(job)
        log.info("fetched=%d candidates=%d new=%d (recorded %d new fetched)",
                 len(all_jobs), len(candidates), len(new_candidates), len(new_fetched))

        if not self._store.is_seeded():
            self._store.save()
            log.info("first run: seeded %d jobs, no scoring or email", len(new_fetched))
            return

        if not new_candidates:
            self._store.save()
            log.info("no new roles this run")
            return

        # Email dedup is by URL (not uid): same job via another source/prior run is skipped.
        # Early-stop dedup stays per-source by uid, so this never affects pagination.
        emailable = self._emailable(new_candidates, known_urls)
        if not emailable:
            self._store.save()
            log.info("%d new roles, all already in the ledger by URL (another source)", len(new_candidates))
            return

        by_track = self._score_by_track(emailable)
        if not by_track:
            self._store.save()
            log.info("%d emailable (of %d new), but none passed a track threshold",
                     len(emailable), len(new_candidates))
            return

        digest = self._build_digest(by_track)
        total = sum(len(items) for _, sections in digest for _, items in sections)
        groups = self._leveler.ordered_groups()
        top_group = groups[0]
        top_count = sum(len(items) for name, sections in digest if name == top_group
                        for _, items in sections)
        subject = f"[Job Scout] {total} new roles"
        if len(groups) > 1 and top_count:
            subject += f" ({top_count} {top_group.lower()})"
        subject += f" [{self._scorer.method_label}]"
        # Timestamp makes each subject unique so Gmail doesn't thread digests together.
        subject += f" {datetime.now(ZoneInfo('America/New_York')):%m/%d %H:%M}"

        try:
            self._notifier.send_digest(digest, subject=subject)
        except Exception as exc:
            # Leave the run unsaved so unsent roles are rediscovered and retried next run.
            log.error("email failed: %s; ledger not saved, roles retry next run", exc)
            return

        emailed = [job.job_uid for _, sections in digest for _, items in sections for job, _ in items]
        self._store.mark_emailed(emailed)
        self._store.save()
        log.info("emailed %d roles (%d %s) across %d groups", total, top_count, top_group.lower(), len(digest))

    def _annotate(self, job: Job) -> Job:
        """Apply the annotator, enforcing its contract (derived presentation fields only):
        a changed job_uid/url would silently corrupt the uid- and URL-keyed dedup downstream,
        so fail loud here instead — the Protocol docstring alone can't."""
        annotated = self._annotator.annotate(job)
        if annotated.job_uid != job.job_uid or annotated.url != job.url:
            raise ValueError(f"annotator changed identity fields for {job.job_uid!r} "
                             f"(uid {annotated.job_uid!r}, url {annotated.url!r})")
        return annotated

    def _suppress_seeding(self, new_candidates: list[Job], known: set[str]) -> list[Job]:
        """Drop candidates from a seed_only source on its FIRST appearance (no uid with its
        prefix was in the ledger before this run) — run() still records them, so a large
        aggregator seeds its backlog silently once, then emails only genuinely new postings."""
        seeding = [p for p in self._seed_only_prefixes
                   if not any(uid.startswith(p) for uid in known)]
        if not seeding:
            return new_candidates
        kept = [job for job in new_candidates
                if not any(job.job_uid.startswith(p) for p in seeding)]
        if len(kept) != len(new_candidates):
            log.info("seeding %d new source(s) silently: withheld %d role(s) from email",
                     len(seeding), len(new_candidates) - len(kept))
        return kept

    def _score_by_track(self, new_jobs: list[Job]) -> dict[str, list[tuple[Job, Score]]]:
        routed: list[tuple[Job, Track]] = []
        for job in new_jobs:
            track = self._router.route(job)
            if track is None:
                log.debug("no track matched: %s (%s)", job.job_uid, job.title)
                continue
            routed.append((job, track))
        if not routed:
            return {}

        total = len(routed)
        workers = min(self._score_workers, total)  # __init__ guarantees score_workers >= 1
        log.info("scoring %d roles across %d workers", total, workers)
        step = max(1, total // 10)  # log progress roughly every 10%

        by_track: dict[str, list[tuple[Job, Score]]] = {}
        # score() blocks (LLM/CLI call; CLI path spawns a subprocess per worker) so it's
        # fanned out over a thread pool. map() preserves submission order -> deterministic
        # digest. Store writes stay on this thread: CsvStore is not concurrency-safe.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for done, attempt in enumerate(pool.map(self._score_one, routed), start=1):
                if done % step == 0 or done == total:
                    log.info("scoring progress: %d/%d", done, total)
                if attempt.score is None:
                    continue
                self._store.set_score(attempt.job.job_uid, attempt.track.name, attempt.score,
                                      method=self._scorer.method_label)
                if attempt.score.relevance_score > attempt.track.threshold:
                    by_track.setdefault(attempt.track.name, []).append((attempt.job, attempt.score))
        return by_track

    def _score_one(self, pair: tuple[Job, Track]) -> _ScoreAttempt:
        job, track = pair
        try:
            return _ScoreAttempt(job, track, self._scorer.score(job, track))
        except Exception as exc:  # unscored rows remain unseeded; retry next run
            log.warning("could not score %s: %s", job.job_uid, exc)
            return _ScoreAttempt(job, track, None)

    @staticmethod
    def _emailable(candidates: list[Job], known_urls: set[str]) -> list[Job]:
        # Email a role once per URL, even across sources: skip a candidate whose URL is
        # already in the ledger or already kept this run. Empty URLs are never deduped
        # (would merge distinct rows). Assumes a URL's keep/drop outcome is source-independent
        # — if two sources disagree on a role's location, one recorded as a non-candidate
        # could block its candidate twin (revisit if that ever bites).
        out: list[Job] = []
        urls: set[str] = set()
        for job in candidates:
            if job.url:
                if job.url in known_urls or job.url in urls:
                    continue
                urls.add(job.url)
            out.append(job)
        return out

    def _build_digest(self, by_track: dict[str, list[tuple[Job, Score]]]) -> Digest:
        grouped: dict[str, dict[str, list[tuple[Job, Score]]]] = {}
        for track_name, items in by_track.items():
            for job, score in items:
                group = self._leveler.group(job)
                grouped.setdefault(group, {}).setdefault(track_name, []).append((job, score))

        digest: Digest = []
        for group_name in self._leveler.ordered_groups():
            track_map = grouped.get(group_name, {})
            sections = []
            for track_name in self._router.ordered_names():
                section_items = track_map.get(track_name)
                if not section_items:
                    continue
                section_items.sort(key=lambda pair: pair[1].experience_score, reverse=True)
                sections.append((track_name, section_items))
            if sections:
                digest.append((group_name, sections))
        return digest

    def _fetch_all(self) -> list[Job]:
        return self._fetcher.fetch_all(self._store.known_uids())
