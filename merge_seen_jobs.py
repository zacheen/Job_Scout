"""One-shot migration: merge one seen_jobs ledger csv into the other.

Dedup/merge logic lives in CsvStore (absorb + merge_rows); this script only
wires the files together.

Direction is selectable: --to data (default) absorbs the stray root
seen_jobs.csv into data/seen_jobs.csv (the config ledger); --to root reverses
it. WARNING: the deleted source may be the ledger the next cloud/local run
reads, which would make that run treat the whole ledger as unseeded — pass
--keep-source when the source csv is still live.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from jobscout.config import Settings
from jobscout.store import CsvStore

ROOT = Path(__file__).resolve().parent


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Merge one seen_jobs ledger csv into the other, then delete the source.")
    parser.add_argument(
        "--to", choices=("data", "root"), default="data",
        help="merge destination: 'data' folds the root seen_jobs.csv into the config "
             "ledger (default); 'root' folds the config ledger into the root csv")
    parser.add_argument(
        "--keep-source", action="store_true",
        help="keep the source csv after merging instead of deleting it")
    args = parser.parse_args()

    settings = Settings.load(ROOT)
    ledger = ROOT / settings.ledger_path
    root_csv = ROOT / "seen_jobs.csv"
    if ledger == root_csv:
        print(f"ledger_path already points at {root_csv}; nothing to merge")
        return 0
    dest, source = (ledger, root_csv) if args.to == "data" else (root_csv, ledger)

    store = CsvStore(dest, track_priority=settings.track_names)
    print(f"{dest}: {len(store)} rows loaded")
    if source.exists():
        store.absorb(source)
        print(f"absorbed {source}: {len(store)} rows after merge")
    else:
        print(f"{source} not found; migrating {dest} schema only")

    store.save()
    print(f"wrote {dest}: {len(store)} rows, {len(store.known_uids())} source uids, "
          f"{len(store.known_urls())} urls")

    if source.exists() and not args.keep_source:
        source.unlink()
        print(f"deleted {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
