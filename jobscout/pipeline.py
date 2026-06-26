"""Orchestrates a single scan run from fetch through to email and persistence."""
from __future__ import annotations

import logging

from .fetchers import AtsFetcher
from .models import Job
from .protocols import JobScorer, JobStore, KeywordMatcher, LocationMatcher, Notifier

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        store: JobStore,
        fetchers: list[AtsFetcher],
        location_filter: LocationMatcher,
        keyword_filter: KeywordMatcher,
        scorer: JobScorer,
        notifier: Notifier,
        cv_threshold: int,
    ):
        self._store = store
        self._fetchers = fetchers
        self._location = location_filter
        self._keyword = keyword_filter
        self._scorer = scorer
        self._notifier = notifier
        self._cv_threshold = cv_threshold

    def run(self) -> None:
        all_jobs = self._fetch_all()
        us_jobs = [j for j in all_jobs if self._location.is_us(j)]
        new_jobs = [j for j in us_jobs if j.job_uid not in self._store.known_uids()]
        log.info("fetched=%d us=%d new=%d", len(all_jobs), len(us_jobs), len(new_jobs))

        for job in new_jobs:
            self._store.add_seen(job)

        if not self._store.is_seeded():
            self._store.save()
            log.info("first run: seeded %d roles, no scoring or email", len(new_jobs))
            return

        selected = self._score_and_select(new_jobs)
        if selected:
            selected.sort(key=lambda pair: pair[1].experience_score, reverse=True)
            subject = f"[Job Scout] {len(selected)} new computer-vision roles"
            self._notifier.send_digest(selected, subject=subject)
            self._store.mark_emailed([job.job_uid for job, _ in selected])
            log.info("emailed %d roles", len(selected))
        else:
            log.info("no roles passed the computer-vision threshold")

        self._store.save()

    def _score_and_select(self, new_jobs: list[Job]):
        candidates = [j for j in new_jobs if self._keyword.matches(j)]
        log.info("scoring %d keyword-matched candidates", len(candidates))
        selected = []
        for job in candidates:
            try:
                score = self._scorer.score(job)
            except Exception as exc:  # unscored rows stay in the store for retry next run
                log.warning("could not score %s: %s", job.job_uid, exc)
                continue
            self._store.set_score(job.job_uid, score)
            if score.computer_vision_score > self._cv_threshold:
                selected.append((job, score))
        return selected

    def _fetch_all(self) -> list[Job]:
        jobs: list[Job] = []
        for fetcher in self._fetchers:
            try:
                jobs.extend(fetcher.fetch())
            except Exception as exc:  # one company's failure must not abort the whole run
                log.warning("fetch failed for an %s company: %s", fetcher.ats_name, exc)
        return jobs
