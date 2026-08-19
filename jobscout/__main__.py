"""Entry point: wire the components together and run one scan."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; env vars still work without it
    load_dotenv = None

from .config import CATCHUP_LOG_FILENAME, Settings
from .coverage import attach_catchup_log
from .fetchers import (AtsFetcher, BambooHrJdSource, ChainedEnricher, DispatchingEnricher,
                       FetcherFactory, HttpClient, JdUrlEnricher, ParallelFetcher,
                       WorkdayJdSource)
from .filters import DescriptionFlagger, LevelClassifier, PreFilter, TrackRouter
from .notifier import EmailNotifier
from .pipeline import Pipeline
from .scoring import build_scorer
from .store import CsvStore


def main(digest_footer: str = "", subject_time: datetime | None = None) -> bool:
    """`digest_footer` is appended to the digest email body and `subject_time`
    stamps the subject line; local_run.py passes a safe-to-delete-window summary
    plus its scan-start time (so subject == window end), cloud runs (run.py)
    leave both unset. Returns whether the run's ledger was saved (see
    Pipeline.run)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parent.parent
    attach_catchup_log(root / CATCHUP_LOG_FILENAME)
    if load_dotenv is not None:
        load_dotenv(root / ".env")  # local dev; no-op in Actions (no .env there)
    settings = Settings.load(root)

    # Referral grouping matches on company name, so a typo / not-yet-added company silently
    # never appears in the Referral group — warn rather than fail quietly.
    known_names = {c.name.strip().lower() for c in settings.companies}
    for rc in settings.referral_companies:
        if rc.strip().lower() not in known_names:
            logging.warning("referral company %r has no companies.yaml entry yet "
                            "(its roles won't be fetched or grouped)", rc)

    # One HttpClient (own session + pacing) per fetcher, so parallel host groups never
    # share a session; same-host fetchers still run sequentially inside ParallelFetcher.
    def make_http() -> HttpClient:
        return HttpClient(
            settings.request_timeout, settings.user_agent,
            settings.request_delay_min, settings.request_delay_max,
        )

    fetchers = [FetcherFactory.create(c, make_http()) for c in settings.companies]
    # Separate from the per-fetcher clients: the enrich stage fetches JD pages on hosts
    # a fetcher may not own at all (an aggregator link can point anywhere).
    jd_http = make_http()
    # seed_only sources (large GitHub aggregators) record their backlog without emailing on
    # first appearance — reuse AtsFetcher.uid_prefix so the uid format lives in one place.
    seed_only_prefixes = {
        AtsFetcher.uid_prefix(c.ats, c.name)
        for c in settings.companies if c.seed_only
    }
    leveler = LevelClassifier(settings.referral_companies, settings.intern_terms,
                              settings.senior_terms)
    scorer, lenient_scorer = build_scorer(settings)

    # Wire the lenient companion (see build_scorer) to the groups worth never missing:
    # it auto-passes title-only roles the keyword fallback would otherwise drop on a
    # near-floor score. The ledger `reason` column records each auto-pass.
    scorer_overrides = {}
    if lenient_scorer is not None:
        scorer_overrides = {leveler.referral_group: lenient_scorer,
                            leveler.intern_group: lenient_scorer}
        logging.info("keyword fallback active: referral/intern title-only roles auto-pass")

    pipeline = Pipeline(
        store=CsvStore(root / settings.ledger_dir, track_priority=settings.track_names),
        fetcher=ParallelFetcher(fetchers),
        prefilter=PreFilter(
            include_location_terms=settings.include_location_terms,
            exclude_location_terms=settings.exclude_location_terms,
            exclude_terms=settings.exclude_terms,
            exclude_dept_terms=settings.exclude_dept_terms,
            exclude_word_terms=settings.exclude_word_terms,
            exclude_description_terms=settings.exclude_description_terms,
            exclude_description_patterns=settings.exclude_description_patterns,
            exempt_role_phrases=settings.exempt_role_phrases,
        ),
        # Backfills descriptions (one per-job detail call) for the few new prefilter
        # survivors whose listing API omitted the body. Two arms, first hit wins:
        # DispatchingEnricher (keyed on which fetcher produced the job, e.g. Oracle),
        # then JdUrlEnricher (keyed on the JD URL's host -- see its docstring for why
        # that's the only handle aggregator rows offer, and why it matters).
        enricher=ChainedEnricher([
            DispatchingEnricher(fetchers),
            # One shared client: unlike the parallel fetch stage, enrichment is a
            # sequential loop, so a second session adds no isolation.
            JdUrlEnricher([WorkdayJdSource(jd_http), BambooHrJdSource(jd_http)]),
        ]),
        annotator=DescriptionFlagger(settings.warn_description_terms),
        router=TrackRouter(settings.tracks),
        leveler=leveler,
        scorer=scorer,
        notifier=EmailNotifier(settings.gmail_user, settings.gmail_app_password, settings.mail_to,
                               footer=digest_footer),
        score_workers=settings.score_workers,
        seed_only_prefixes=seed_only_prefixes,
        scorer_overrides=scorer_overrides,
        # Senior roles are dropped (not emailed) unless a referral company claims them first:
        # LevelClassifier routes a referral-company senior role to Referral, not Senior.
        suppressed_groups={leveler.senior_group},
        subject_time=subject_time,
    )
    return pipeline.run()


if __name__ == "__main__":
    main()
