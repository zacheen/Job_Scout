"""Orchestrates a single scan run: fetch -> filter -> route -> score -> one digest email."""
from __future__ import annotations

import logging

from .fetchers import AtsFetcher
from .models import Job, Score
from .protocols import JobFilter, JobScorer, JobStore, Notifier, Router

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        store: JobStore,
        fetchers: list[AtsFetcher],
        prefilter: JobFilter,
        router: Router,
        scorer: JobScorer,
        notifier: Notifier,
    ):
        self._store = store
        self._fetchers = fetchers
        self._prefilter = prefilter
        self._router = router
        self._scorer = scorer
        self._notifier = notifier

    def run(self) -> None:
        all_jobs = self._fetch_all()
        candidates = [j for j in all_jobs if self._prefilter.keep(j)]
        new_jobs = [j for j in candidates if j.job_uid not in self._store.known_uids()]
        log.info("fetched=%d candidates=%d new=%d", len(all_jobs), len(candidates), len(new_jobs))

        for job in new_jobs:
            self._store.add_seen(job)

        if not self._store.is_seeded():
            self._store.save()
            log.info("first run: seeded %d roles, no scoring or email", len(new_jobs))
            return

        by_track = self._score_by_track(new_jobs)
        if not by_track:
            log.info("no roles passed any track threshold")
            self._store.save()
            return

        # One email; sections in track (priority) order, each sorted by experience.
        sections = [(name, by_track[name]) for name in self._router.ordered_names() if name in by_track]
        for _, items in sections:
            items.sort(key=lambda pair: pair[1].experience_score, reverse=True)
        total = sum(len(items) for _, items in sections)

        try:
            self._notifier.send_digest(sections, subject=f"[Job Scout] {total} new roles")
        except Exception as exc:
            # Leave the run unsaved so unsent roles are rediscovered and retried next run.
            log.error("email failed: %s; ledger not saved, roles retry next run", exc)
            return

        self._store.mark_emailed([job.job_uid for _, items in sections for job, _ in items])
        self._store.save()
        log.info("emailed %d roles across %d sections", total, len(sections))

    def _score_by_track(self, new_jobs: list[Job]) -> dict[str, list[tuple[Job, Score]]]:
        by_track: dict[str, list[tuple[Job, Score]]] = {}
        for job in new_jobs:
            track = self._router.route(job)
            if track is None:
                log.debug("no track matched: %s (%s)", job.job_uid, job.title)
                continue
            try:
                score = self._scorer.score(job, track)
            except Exception as exc:  # unscored rows remain unseeded; retry next run
                log.warning("could not score %s: %s", job.job_uid, exc)
                continue
            self._store.set_score(job.job_uid, track.name, score)
            if score.relevance_score > track.threshold:
                by_track.setdefault(track.name, []).append((job, score))
        return by_track

    def _fetch_all(self) -> list[Job]:
        # Pass known uids so date-ordered fetchers can stop paginating early.
        seen = self._store.known_uids()
        jobs: list[Job] = []
        for fetcher in self._fetchers:
            try:
                jobs.extend(fetcher.fetch(seen))
            except Exception as exc:  # one company failing must not abort the whole run
                log.warning("fetch failed for an %s company: %s", fetcher.ats_name, exc)
        return jobs
