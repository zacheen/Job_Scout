"""Orchestrates a single scan run: fetch -> filter -> route -> score -> one digest email."""
from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import NamedTuple

from collections.abc import Collection, Mapping

from .config import DIGEST_TZ, Track
from .models import Job, Score
from .protocols import (Annotator, Digest, Enricher, Fetcher, JobFilter, JobScorer, JobStore,
                        Leveler, Notifier, Router)
from .urls import canon_url

log = logging.getLogger(__name__)


class _ScoreAttempt(NamedTuple):
    job: Job
    track: Track
    score: Score | None  # None = scoring failed (already logged, not re-raised)
    method: str  # method_label of the scorer that actually ran (group overrides may differ)


class Pipeline:
    def __init__(
        self,
        *,
        store: JobStore,
        fetcher: Fetcher,
        prefilter: JobFilter,
        enricher: Enricher | None = None,
        annotator: Annotator,
        router: Router,
        leveler: Leveler,
        scorer: JobScorer,
        notifier: Notifier,
        score_workers: int = 1,
        seed_only_prefixes: Collection[str] = (),
        scorer_overrides: Mapping[str, JobScorer] | None = None,
        suppressed_groups: Collection[str] = (),
        subject_time: datetime | None = None,
    ):
        if score_workers < 1:
            raise ValueError(f"score_workers must be >= 1, got {score_workers}")
        self._store = store
        self._fetcher = fetcher
        self._prefilter = prefilter
        # None = no source offers per-job detail; the enrich stage becomes a no-op.
        self._enricher = enricher
        self._annotator = annotator
        self._router = router
        self._leveler = leveler
        self._scorer = scorer
        self._notifier = notifier
        self._score_workers = score_workers
        # uid prefixes of sources that seed silently on their first appearance (see run()).
        self._seed_only_prefixes = tuple(seed_only_prefixes)
        # Per-group scorer overrides: a job whose leveler group is listed here is scored
        # by that scorer instead of `scorer`. Email subject always shows the primary
        # scorer's label; the ledger records the actual method used per row.
        self._scorer_overrides = dict(scorer_overrides or {})
        # Leveler group names dropped before scoring/email (see `_drop_suppressed`).
        self._suppressed_groups = frozenset(suppressed_groups)
        # Subject-line timestamp; None = stamped at send time. local_run.py
        # passes its scan-start time so the subject matches the footer's
        # deletable-window end exactly (one cutoff for the user).
        self._subject_time = subject_time

    def run(self) -> bool:
        """Returns True once this run's findings are durably saved to the ledger.
        False only on the email-send failure path, where the ledger is
        deliberately left unsaved so unemailed roles retry next run — callers
        gating side effects on "did local_data catch up" (local_run.py's digest
        checkpoint) must treat False as "it did not"."""
        # One ledger snapshot serves both jobs: it bounds each company's pagination and
        # is the dedupe baseline. Taken BEFORE add_seen, so this run's own jobs don't dedup
        # themselves — and so no company's watermark has advanced to today yet.
        ledger = self._store.seen_ledger()
        all_jobs = self._drop_untitled(self._fetcher.fetch_all(ledger))
        known = ledger.uids
        known_urls = self._store.known_urls()
        candidates = [j for j in all_jobs if self._prefilter.keep(j)]
        new_candidates = [j for j in candidates if j.job_uid not in known]
        new_candidates = self._suppress_seeding(new_candidates, known)
        # Record all new fetched jobs, not only candidates: PreFilter-rejected jobs are
        # otherwise never recorded and look "new" forever, defeating early-stop.
        new_fetched = [j for j in all_jobs if j.job_uid not in known]
        # Re-stamped roles: uid already recorded, but the board now shows a different
        # posting date. Written back so seen_snapshot stops flagging them — left alone they
        # would fail that test on every future run and the already-seen stop could never
        # fire on a board that re-stamps in bulk. Not added to `new_candidates`, so a
        # re-stamp never re-emails a role. add_seen routes through merge_rows, which keeps
        # the earlier first_seen and the better score, so nothing is lost by rewriting.
        restamped = [j for j in all_jobs if j.job_uid in known
                     and not ledger.seen_snapshot(j.job_uid, j.date_posted)]
        for job in new_fetched + restamped:
            self._store.add_seen(job)
        log.info("fetched=%d candidates=%d new=%d (recorded %d new fetched, %d re-stamped)",
                 len(all_jobs), len(candidates), len(new_candidates), len(new_fetched),
                 len(restamped))

        if not self._store.is_seeded():
            self._store.save()
            log.info("first run: seeded %d jobs, no scoring or email", len(new_fetched))
            return True

        if not new_candidates:
            self._store.save()
            log.info("no new roles this run")
            return True

        # Enrich + annotate only the genuinely new candidates (steady-state runs mostly
        # refetch already-known jobs), and only after the seeding early-returns above —
        # enrichment costs one network call per job. Enriched/annotated copies flow only
        # to the email path; add_seen above recorded the originals from all_jobs.
        new_candidates = [self._annotate(j) for j in self._enrich_and_refilter(new_candidates)]
        if not new_candidates:
            self._store.save()
            log.info("all new roles dropped on their detail-fetched text")
            return True

        # Email dedup is by URL (not uid): same job via another source/prior run is skipped.
        # Early-stop dedup stays per-source by uid, so this never affects pagination.
        emailable = self._emailable(new_candidates, known_urls)
        if not emailable:
            self._store.save()
            log.info("%d new roles, all already in the ledger by URL (another source)", len(new_candidates))
            return True

        before_suppress = len(emailable)
        emailable = self._drop_suppressed(emailable)
        if not emailable:
            self._store.save()
            log.info("%d emailable (of %d new), all in suppressed groups (e.g. senior at a non-referral company)",
                     before_suppress, len(new_candidates))
            return True

        by_track = self._score_by_track(emailable)
        if not by_track:
            self._store.save()
            log.info("%d emailable (of %d new), but none passed a track threshold",
                     len(emailable), len(new_candidates))
            return True

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
        subject += f" {(self._subject_time or datetime.now(DIGEST_TZ)):%m/%d %H:%M}"

        try:
            self._notifier.send_digest(digest, subject=subject)
        except Exception as exc:
            # Leave the run unsaved so unsent roles are rediscovered and retried next run.
            log.error("email failed: %s; ledger not saved, roles retry next run", exc)
            return False

        emailed = [job.job_uid for _, sections in digest for _, items in sections for job, _ in items]
        self._store.mark_emailed(emailed)
        self._store.save()
        log.info("emailed %d roles (%d %s) across %d groups", total, top_count, top_group.lower(), len(digest))
        return True

    @staticmethod
    def _drop_untitled(jobs: list[Job]) -> list[Job]:
        """Discard postings the source returned with no title, before run() records them.
        Recording one is permanent damage: add_seen retires the uid — the posting's real
        board id — so when the same req comes back with its title populated it reads as
        already-seen and is never scored or emailed. Only Workday has produced these (121
        ledger rows by 2026-08-19, ~1-6/day): its listing API intermittently returns an
        item carrying nothing but externalPath, while a later request serves the same req
        with its title, so dropping costs one refetch and recovers the role. Deriving the
        title from the URL slug instead was rejected — Workday freezes the slug at
        requisition creation, so it disagrees with the live title on 8% of the ledger's
        86k Workday rows."""
        kept, untitled = [], []
        for job in jobs:
            (kept if job.title.strip() else untitled).append(job)
        if untitled:
            log.warning("dropped %d untitled posting(s), left unrecorded so a later run can "
                        "still score them: %s", len(untitled),
                        Counter(job.company for job in untitled).most_common())
        return kept

    def _annotate(self, job: Job) -> Job:
        return self._same_identity(self._annotator.annotate(job), job, "annotator")

    def _enrich_and_refilter(self, jobs: list[Job]) -> list[Job]:
        """Fill each job's costly-to-fetch fields from its source's per-job detail endpoint
        (a no-op for most sources), then re-run the prefilter on the touched jobs: exclude
        boilerplate (e.g. "no visa sponsorship") often lives only in the detail text that
        the listing API omitted. Enrichment fails open (Enricher contract), so a fetch
        error just leaves a job on its listing-level text."""
        if self._enricher is None:
            return jobs
        kept = []
        for job in jobs:
            try:
                enriched = self._enricher.enrich(job)
            except Exception as exc:  # enricher broke its fail-open contract; honor it here
                log.warning("enrich failed for %s, keeping listing-level text: %s",
                            job.job_uid, exc)
                enriched = job
            # Outside the try: an identity violation is a programming error — fail loud.
            enriched = self._same_identity(enriched, job, "enricher")
            if enriched is not job and not self._prefilter.keep(enriched):
                log.info("dropped on detail-fetched text: %s (%s)", job.job_uid, job.title)
                continue
            kept.append(enriched)
        return kept

    @staticmethod
    def _same_identity(derived: Job, original: Job, component: str) -> Job:
        """Enforce the Annotator/Enricher contract (derived fields only): a changed
        job_uid/url would silently corrupt the uid- and URL-keyed dedup downstream, so
        fail loud here instead — the Protocol docstrings alone can't."""
        if derived.job_uid != original.job_uid or derived.url != original.url:
            raise ValueError(f"{component} changed identity fields for {original.job_uid!r} "
                             f"(uid {derived.job_uid!r}, url {derived.url!r})")
        return derived

    def _suppress_seeding(self, new_candidates: list[Job], known: Collection[str]) -> list[Job]:
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

    def _drop_suppressed(self, jobs: list[Job]) -> list[Job]:
        """Drop jobs whose leveler group is suppressed (never emailed) — e.g. senior roles.
        A job reaches a suppressed group only if no higher-priority group claimed it first (a
        senior role at a referral company lands in Referral instead, so it survives). run()
        has already recorded these in the ledger; they're simply never scored or emailed."""
        if not self._suppressed_groups:
            return jobs
        kept = [j for j in jobs if self._leveler.group(j) not in self._suppressed_groups]
        dropped = len(jobs) - len(kept)
        if dropped:
            log.info("suppressed %d role(s) in group(s) %s (not emailed)",
                     dropped, sorted(self._suppressed_groups))
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
                                      method=attempt.method)
                if attempt.score.experience_score > attempt.track.threshold_for(attempt.score.scale):
                    by_track.setdefault(attempt.track.name, []).append((attempt.job, attempt.score))
        return by_track

    def _score_one(self, pair: tuple[Job, Track]) -> _ScoreAttempt:
        job, track = pair
        scorer = self._scorer_overrides.get(self._leveler.group(job), self._scorer)
        try:
            return _ScoreAttempt(job, track, scorer.score(job), scorer.method_label)
        except Exception as exc:  # unscored rows remain unseeded; retry next run
            log.warning("could not score %s: %s", job.job_uid, exc)
            return _ScoreAttempt(job, track, None, scorer.method_label)

    @staticmethod
    def _emailable(candidates: list[Job], known_urls: set[str]) -> list[Job]:
        # Email a role once per URL, even across sources: skip a candidate whose URL is
        # already in the ledger or already kept this run (compared via canon_url, matching
        # known_urls' keys). Empty URLs are never deduped (would merge distinct rows).
        # Assumes a URL's keep/drop outcome is source-independent — if two sources
        # disagree on a role's location, one recorded as a non-candidate could block its
        # candidate twin (revisit if that ever bites).
        out: list[Job] = []
        urls: set[str] = set()
        for job in candidates:
            if job.url:
                key = canon_url(job.url)
                if key in known_urls or key in urls:
                    continue
                urls.add(key)
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
                # Tie-break on matches: keyword-scored items' experience_score all
                # clamp to 100, so this must match what the email displays.
                section_items.sort(key=lambda pair: (pair[1].experience_score,
                                                     pair[1].matches or 0), reverse=True)
                sections.append((track_name, section_items))
            if sections:
                digest.append((group_name, sections))
        return digest
