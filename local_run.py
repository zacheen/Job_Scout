"""Local wrapper around run.py that keeps the git-hosted ledger in sync.

Cloud runs (scan.yml) keep their ledger in seen_jobs.csv at the ROOT of the
`data` branch; local runs use data/seen_jobs.csv (config default). Running
either side without syncing re-emails roles the other side already saw, so
this script wraps a scan with a pull / union-merge / push cycle:

    1. refuse to run unless the checkout is on the `data` branch
    2. fetch + fast-forward to origin/data (grabs the cloud's latest csv)
    3. union-merge root csv <-> data/ csv (CsvStore.absorb) into BOTH files,
       so the scan knows every role the cloud has seen or emailed
    4. run the normal scan (jobscout main, same as run.py)
    5. merge again and commit + push both csvs; if the cloud pushed while we
       were scanning, re-merge and retry the push once

NOT merge_seen_jobs.py: that script is a one-shot migration — it absorbs one
csv into the other (direction picked via --to) and then DELETES the source csv,
which would make the next cloud/local run treat the whole ledger as unseeded.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; env vars still work without it
    load_dotenv = None

from jobscout.config import Settings
from jobscout.store import CsvStore

ROOT = Path(__file__).resolve().parent
BRANCH = "data"
CLOUD_CSV = ROOT / "seen_jobs.csv"  # scan.yml's LEDGER_FILE, at the data-branch root

log = logging.getLogger("local_run")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in the repo root, echoing output to the console."""
    proc = subprocess.run(["git", "-C", str(ROOT), *args])
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed (exit {proc.returncode})")
    return proc


def _git_out(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _ensure_data_branch() -> None:
    branch = _git_out("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise SystemExit(f"local_run.py must run on the {BRANCH!r} branch "
                         f"(currently on {branch!r}); the ledger csvs only live there")


def _ledger_csvs(settings: Settings) -> list[Path]:
    """The local ledger first, then the cloud csv (deduped when they coincide)."""
    local = ROOT / settings.ledger_path
    return [local] if local == CLOUD_CSV else [local, CLOUD_CSV]


def _merge_ledgers(settings: Settings, extra: list[Path] = []) -> None:
    """CsvStore.absorb dedupes by job_key/URL and keeps emailed=true on merge,
    so this is idempotent regardless of which csv is treated as primary."""
    csvs = _ledger_csvs(settings)
    store = CsvStore(csvs[0], track_priority=settings.track_names)
    for path in csvs[1:] + extra:
        if path.exists():
            store.absorb(path)
    store.save()
    for path in csvs[1:]:
        shutil.copyfile(csvs[0], path)


def _sync_with_remote(settings: Settings) -> None:
    """Fast-forward to origin/data without losing local ledger rows: snapshot the
    csvs, restore the committed versions (so the merge can't hit conflicts), then
    fold the snapshots back into the fast-forwarded files."""
    _git("fetch", "origin", BRANCH)
    snapshots: list[Path] = []
    tmp = Path(tempfile.mkdtemp(prefix="jobscout_ledger_"))
    for i, path in enumerate(_ledger_csvs(settings)):
        if path.exists():
            snap = tmp / f"snapshot_{i}.csv"
            shutil.copyfile(path, snap)
            snapshots.append(snap)
            # check=False: an untracked csv (first run ever) has nothing to restore
            _git("checkout", "--", str(path), check=False)
    _git("merge", "--ff-only", f"origin/{BRANCH}")
    _merge_ledgers(settings, extra=snapshots)
    shutil.rmtree(tmp, ignore_errors=True)


def _commit_and_push(settings: Settings) -> None:
    rel_paths = [str(p.relative_to(ROOT)) for p in _ledger_csvs(settings)]
    for attempt in (1, 2):
        if not _git_out("status", "--porcelain", "--", *rel_paths):
            log.info("ledger unchanged; nothing to push")
            return
        # pathspec commit: only the ledger csvs, never other staged/dirty files
        _git("commit", "-m", "update job ledger (local run)", "--", *rel_paths)
        if _git("push", "origin", BRANCH, check=False).returncode == 0:
            log.info("ledger pushed to origin/%s", BRANCH)
            return
        if attempt == 1:
            # the cloud run pushed while we scanned: undo our commit (content
            # stays in the working tree), re-merge with the new remote tip, retry
            log.warning("push rejected; merging remote changes and retrying")
            _git("reset", "--mixed", "HEAD~1")
            _sync_with_remote(settings)
    raise SystemExit("push failed twice; resolve manually (git pull --rebase, "
                     "then re-run local_run.py)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _ensure_data_branch()
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")  # before Settings.load so LEDGER_FILE etc. apply
    settings = Settings.load(ROOT)

    _sync_with_remote(settings)  # pre-scan: learn what the cloud already saw/emailed

    from jobscout.__main__ import main as run_scan  # same entry as run.py
    run_scan()  # on failure the ledger is unsaved; propagate and skip the push

    _sync_with_remote(settings)  # post-scan: fold in anything the cloud pushed meanwhile
    _commit_and_push(settings)


if __name__ == "__main__":
    sys.exit(main())
