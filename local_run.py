"""Local wrapper around run.py that keeps the git-hosted ledger in sync.

Cloud runs (scan.yml) keep their ledger in cloud_data/ at the ROOT of the `data`
branch; local runs use local_data/ (config default). Both are directories of
per-company CSV shards. Automatic sync is ONE-WAY, local -> cloud: the cloud
must learn every role a local scan saw or emailed (so it never re-emails them),
but local_data/ never absorbs cloud rows here. Folding cloud_data/ back into
local_data/ is a deliberate manual step — run merge_seen_jobs.py (default
direction) — so roles the cloud already emailed KEEP re-surfacing (and
re-emailing) in local scans until you fold them in.

    0. refuse to start while another ledger script holds the lock — two runs
       hard-resetting and pushing the same repo corrupt both the ledger and
       the git object store (gitledger.acquire_single_instance_lock)
    1. refuse to run unless the checkout is on the `data` branch and step 2's
       hard-reset cannot destroy work: no uncommitted changes or unpushed
       commits touching files outside the shard dirs (gitledger.ensure_reset_safe)
    2. fetch + hard-reset to origin/data (the cloud amends + force-pushes its
       ledger commit, so origin/data routinely rewrites history and a
       fast-forward would fail); each shard dir is snapshotted first and
       re-absorbs its own snapshot after the reset, so no rows are lost
    3. union-merge local_data/ INTO cloud_data/ (never the reverse)
    4. run the normal scan (jobscout main, same as run.py)
    5. merge again and commit + push both dirs; if the cloud pushed while we
       were scanning, re-merge and retry the push once
    6. delete the packs this run's auto-gc could not unlink, which it cannot
       report now that git has no console to ask on (gitledger.sweep_pack_garbage)

Steps 0, 1, 2 and 5 are git plumbing shared with merge_seen_jobs.py — see
jobscout/gitledger.py. Only the merge policy (_merge_ledgers) is local to here.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; env vars still work without it
    load_dotenv = None

from jobscout import gitledger
from jobscout.config import DIGEST_CHECKPOINT_FILENAME, DIGEST_TZ, Settings
from jobscout.store import union_merge

ROOT = Path(__file__).resolve().parent
SCRIPT = Path(__file__).name
CLOUD_DIR = ROOT / "cloud_data"  # scan.yml's LEDGER_DIR, at the data-branch root
CHECKPOINT = ROOT / DIGEST_CHECKPOINT_FILENAME  # untracked: survives the hard-resets below

log = logging.getLogger("local_run")


def _read_checkpoint() -> datetime | None:
    """The digest footer's deletable-window lower bound: the last moment
    local_data caught up with cloud-emailed roles — a local scan whose ledger
    was saved (stamped in main()) or a manual cloud->local fold (stamped by
    merge_seen_jobs.py). Cloud digests at or before it never re-surface in
    local digests, so the footer flags them "review before deleting".
    Missing/corrupt stamp -> None: the footer then claims every earlier digest
    is covered, which overstates once after a re-clone; the stamp self-heals
    when this run finishes."""
    try:
        raw = CHECKPOINT.read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(raw).astimezone(DIGEST_TZ)
    except (OSError, ValueError):
        return None


def _write_checkpoint(moment: datetime) -> None:
    CHECKPOINT.write_text(moment.isoformat(timespec="seconds") + "\n", encoding="utf-8")


def _digest_footer(checkpoint: datetime | None, start: datetime) -> str:
    """The digest's "safe to delete" window; _read_checkpoint explains the
    lower bound. `start` is also the run's subject_time, so the window end
    and the subject timestamp are the same instant (and the next digest's
    lower bound is this digest's subject time)."""
    fmt = "%Y-%m-%d %H:%M"
    tail = f"({DIGEST_TZ.key}) — safe to delete those."
    if checkpoint is None:
        return f"Covers every cloud digest sent before {start:{fmt}} {tail}"
    return (f"Covers cloud digests sent between {checkpoint:{fmt}} and {start:{fmt}} {tail} "
            f"Digests from before {checkpoint:{fmt}} were already covered by an earlier "
            "local digest or folded into local data (merge_seen_jobs.py); review them "
            "before deleting.")


def _ledger_dirs(settings: Settings) -> list[Path]:
    """The local shard dir first, then the cloud dir (deduped when they coincide)."""
    local = ROOT / settings.ledger_dir
    return [local] if local == CLOUD_DIR else [local, CLOUD_DIR]


def _merge_ledgers(settings: Settings, snapshots: list[list[Path]]) -> None:
    """One-way sync: each dir re-absorbs its own snapshot (rows the hard-reset
    threw away), then cloud_data absorbs local_data so the cloud never re-emails
    a role a local scan already saw. local_data absorbs nothing from the cloud —
    that direction is manual (merge_seen_jobs.py). union_merge dedupes by
    job_key/canonical URL and keeps emailed=true on merge, so this is idempotent."""
    dirs = _ledger_dirs(settings)
    union_merge(dirs[0], settings.track_names, extra_files=snapshots[0])
    for d, snap in zip(dirs[1:], snapshots[1:]):
        union_merge(d, settings.track_names, extra_dirs=[dirs[0]], extra_files=snap)


def _sync_with_remote(settings: Settings) -> None:
    gitledger.sync_with_remote(ROOT, _ledger_dirs(settings),
                               lambda snaps: _merge_ledgers(settings, snaps))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # before any git call: a second run must not even fetch
    gitledger.acquire_single_instance_lock(ROOT)
    gitledger.ensure_branch(ROOT, SCRIPT)
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")  # before Settings.load so LEDGER_DIR etc. apply
    settings = Settings.load(ROOT)
    gitledger.ensure_reset_safe(ROOT, _ledger_dirs(settings), SCRIPT)

    _sync_with_remote(settings)  # pre-scan: align with origin/data so the final push can fast-forward

    # This scan's start doubles as the checkpoint window's end: any cloud digest
    # sent before it covered roles that were still live then, so this scan
    # independently re-finds and re-emails them — except roles local_data
    # already knew (the window's lower bound, see _read_checkpoint).
    start = datetime.now(DIGEST_TZ)

    from jobscout.__main__ import main as run_scan  # same entry as run.py
    # Only an uncaught exception here skips the push — the handled saved=False
    # case below still pushes, just without advancing the checkpoint.
    # subject_time=start: the subject timestamp IS the deletable-window end, so
    # "delete every cloud digest older than this subject" needs no footer math.
    saved = run_scan(digest_footer=_digest_footer(_read_checkpoint(), start),
                     subject_time=start)
    if saved:
        _write_checkpoint(start)  # ledger saved: the next window starts where this scan began
    else:
        log.warning("digest email failed; ledger unsaved, checkpoint not advanced "
                    "(unemailed roles retry next run)")

    _sync_with_remote(settings)  # post-scan: re-align with the remote tip, fold scan finds into cloud_data
    gitledger.commit_and_push(ROOT, _ledger_dirs(settings), "update job ledger (local run)",
                              lambda snaps: _merge_ledgers(settings, snaps))
    # after the last git call, still holding the lock: no repack of ours can be
    # mid-rename, so an .idx-less .pack here is garbage and not a newborn
    gitledger.sweep_pack_garbage(ROOT)


if __name__ == "__main__":
    sys.exit(main())
