"""Orchestrates a single scan run: fetch -> filter -> route -> score -> one digest email."""
from __future__ import annotations

import logging

from .models import Job, Score
from .protocols import Digest, Fetcher, JobFilter, JobScorer, JobStore, Leveler, Notifier, Router

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        store: JobStore,
        fetcher: Fetcher,
        prefilter: JobFilter,
        router: Router,
        leveler: Leveler,
        scorer: JobScorer,
        notifier: Notifier,
    ):
        self._store = store
        self._fetcher = fetcher
        self._prefilter = prefilter
        self._router = router
        self._leveler = leveler
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

        digest = self._build_digest(by_track)
        total = sum(len(items) for _, sections in digest for _, items in sections)
        groups = self._leveler.ordered_groups()
        top_group = groups[0]
        top_count = sum(len(items) for name, sections in digest if name == top_group
                        for _, items in sections)
        subject = f"[Job Scout] {total} new roles"
        # Call out the top group only when it's a distinct priority group (e.g. intern),
        # not when there's a single catch-all group.
        if len(groups) > 1 and top_count:
            subject += f" ({top_count} {top_group.lower()})"

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

    def _build_digest(self, by_track: dict[str, list[tuple[Job, Score]]]) -> Digest:
        # Re-bucket each track's items by level group, then emit group -> track sections
        # in (leveler, router) priority order, each section ranked by experience.
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
