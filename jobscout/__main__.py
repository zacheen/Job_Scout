"""Entry point: wire the components together and run one scan."""
from __future__ import annotations

import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; env vars still work without it
    load_dotenv = None

from .config import Settings
from .fetchers import AtsFetcher, FetcherFactory, HttpClient, ParallelFetcher
from .filters import DescriptionFlagger, LevelClassifier, PreFilter, TrackRouter
from .notifier import EmailNotifier
from .pipeline import Pipeline
from .scoring import CliScorer, KeywordScorer, build_scorer
from .store import CsvStore


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parent.parent
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
    # seed_only sources (large GitHub aggregators) record their backlog without emailing on
    # first appearance — reuse AtsFetcher.uid_prefix so the uid format lives in one place.
    seed_only_prefixes = {
        AtsFetcher.uid_prefix(c.ats, c.name)
        for c in settings.companies if c.seed_only
    }
    leveler = LevelClassifier(settings.referral_companies, settings.intern_terms,
                              settings.senior_terms)
    scorer = build_scorer(settings)
    # Senior roles render last in the email and are usually skimmed past, so they don't
    # justify a slow local CLI subprocess call: swap in the keyword heuristic for them
    # on the CLI path. API path is unaffected — it scores every group.
    scorer_overrides = {}
    if settings.senior_terms and isinstance(scorer, CliScorer):
        scorer_overrides[leveler.senior_group] = KeywordScorer(settings.skill_keywords)

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
        ),
        annotator=DescriptionFlagger(settings.warn_description_terms),
        router=TrackRouter(settings.tracks),
        leveler=leveler,
        scorer=scorer,
        notifier=EmailNotifier(settings.gmail_user, settings.gmail_app_password, settings.mail_to),
        score_workers=settings.score_workers,
        seed_only_prefixes=seed_only_prefixes,
        scorer_overrides=scorer_overrides,
    )
    pipeline.run()


if __name__ == "__main__":
    main()
