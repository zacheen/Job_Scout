"""Entry point: wire the components together and run one scan."""
from __future__ import annotations

import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; env vars still work without it
    load_dotenv = None

from .config import Settings
from .fetchers import FetcherFactory, HttpClient
from .filters import KeywordFilter, LocationFilter
from .notifier import EmailNotifier
from .pipeline import Pipeline
from .scoring import build_scorer
from .store import CsvStore


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parent.parent
    if load_dotenv is not None:
        load_dotenv(root / ".env")  # local dev; no-op in Actions (no .env there)
    settings = Settings.load(root)

    http = HttpClient(settings.request_timeout, settings.user_agent)
    pipeline = Pipeline(
        store=CsvStore(root / "data" / "seen_jobs.csv"),
        fetchers=[FetcherFactory.create(c, http) for c in settings.companies],
        location_filter=LocationFilter(settings.location_us_terms),
        keyword_filter=KeywordFilter(settings.keywords),
        scorer=build_scorer(settings),
        notifier=EmailNotifier(settings.gmail_user, settings.gmail_app_password, settings.mail_to),
        cv_threshold=settings.cv_threshold,
    )
    pipeline.run()


if __name__ == "__main__":
    main()
