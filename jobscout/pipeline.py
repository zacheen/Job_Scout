"""Orchestrates a single scan run: fetch -> filter -> route -> score -> per-track email."""
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

        emailed: list[str] = []
        all_sent = True
        for track_name, items in by_track.items():
            items.sort(key=lambda pair: pair[1].experience_score, reverse=True)
            try:
                self._notifier.send_digest(items, subject=f"[Job Scout] {len(items)} new {track_name} roles")
            except Exception as exc:
                all_sent = False
                log.error("email failed for %s track: %s", track_name, exc)
                continue
            emailed.extend(job.job_uid for job, _ in items)
            log.info("emailed %d %s roles", len(items), track_name)

        # At-least-once delivery: if any digest failed, do NOT save the ledger so those
        # roles are retried next run. Trade-off: tracks that DID send may be re-emailed.
        if all_sent:
            self._store.mark_emailed(emailed)
            self._store.save()
        else:
            log.error("ledger not saved — email failed; affected roles will retry next run")

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
        jobs: list[Job] = []
        for fetcher in self._fetchers:
            try:
                jobs.extend(fetcher.fetch())
            except Exception as exc:  # one company failing must not abort the whole run
                log.warning("fetch failed for an %s company: %s", fetcher.ats_name, exc)
        return jobs
