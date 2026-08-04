"""Audit list of roles that never reached the inbox: title, company, department.

No new pipeline recording is needed: run() already writes EVERY fetched role into
the ledger shards — including PreFilter rejects — so this script only projects the
existing CSVs (default cloud_data/, the cloud-committed record) into a plain-text
file, one role per line, split into two sections:

  scored below threshold  -> scored=true,  emailed=false  (keyword/LLM gate said no)
  dropped before scoring  -> scored=false, emailed=false  (prefiltered, no track hit,
                             URL-deduped, senior-suppressed, or seed-only backlog)

The ledger does not record WHICH pre-scoring cause applied; a company's
first-appearance seed backlog dominates the second section, so use --days to cut
the list down to recent runs when hunting for wrongly filtered roles.

Run from a checkout of the `data` branch (the shard dirs only live there).
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_rows(ledger: Path) -> list[dict]:
    rows: list[dict] = []
    for shard in sorted(ledger.glob("*.csv")):
        with shard.open(encoding="utf-8", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def _line(row: dict) -> str:
    return f"{row['title']}, {row['company']}, {row.get('department') or '-'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", default="cloud_data",
                        help="shard dir to read, relative to the repo root or absolute "
                             "(default: cloud_data)")
    parser.add_argument("--days", type=int, default=3, metavar="N",
                        help="only roles first seen within the last N days "
                             "(UTC date cutoff; 0 = everything)")
    parser.add_argument("--out", default="dropped_jobs.txt",
                        help="output txt path (default: dropped_jobs.txt, gitignored)")
    args = parser.parse_args()

    ledger = ROOT / args.ledger
    if not ledger.is_dir():
        print(f"{ledger} not found — run this from a checkout of the `data` branch",
              file=sys.stderr)
        return 1

    rows = _load_rows(ledger)
    if args.days > 0:
        # first_seen is ISO 8601 UTC, so a YYYY-MM-DD prefix comparison is date order;
        # rows without one predate the column and are treated as old (excluded).
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).date().isoformat()
        rows = [r for r in rows if r.get("first_seen", "")[:10] >= cutoff]
    dropped = [r for r in rows if r.get("emailed") != "true"]
    dropped.sort(key=lambda r: r.get("first_seen", ""), reverse=True)  # newest first
    gated = [r for r in dropped if r.get("scored") == "true"]
    unscored = [r for r in dropped if r.get("scored") != "true"]

    out = Path(args.out)
    with out.open("w", encoding="utf-8") as fh:
        fh.write(f"=== scored below threshold, not emailed: {len(gated)} ===\n")
        fh.writelines(_line(r) + "\n" for r in gated)
        fh.write(f"\n=== dropped before scoring (prefiltered / no track / dedup / "
                 f"seeded): {len(unscored)} ===\n")
        fh.writelines(_line(r) + "\n" for r in unscored)

    scope = f" (last {args.days} days)" if args.days > 0 else ""
    print(f"{len(gated)} scored-but-not-emailed + {len(unscored)} dropped-before-scoring"
          f"{scope} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
