"""One-shot migration: merge the stray root seen_jobs.csv into data/seen_jobs.csv.

All dedup/merge logic lives in CsvStore (absorb + merge_rows); this script only
wires files together, prints stats, and removes the stray copy on success.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from jobscout.config import Settings
from jobscout.store import CsvStore

ROOT = Path(__file__).resolve().parent


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings.load(ROOT)
    ledger = ROOT / settings.ledger_path
    stray = ROOT / "seen_jobs.csv"

    store = CsvStore(ledger, track_priority=settings.track_names)
    print(f"{ledger}: {len(store)} rows loaded")
    if stray.exists():
        store.absorb(stray)
        print(f"absorbed {stray}: {len(store)} rows after merge")
    else:
        print(f"{stray} not found; migrating {ledger} schema only")

    store.save()
    print(f"wrote {ledger}: {len(store)} rows, {len(store.known_uids())} source uids, "
          f"{len(store.known_urls())} urls")

    if stray.exists():
        stray.unlink()
        print(f"deleted {stray}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
