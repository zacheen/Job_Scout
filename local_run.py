"""Local wrapper around run.py that keeps the git-hosted ledger in sync.

Cloud runs (scan.yml) keep their ledger in cloud_data/ at the ROOT of the `data`
branch; local runs use local_data/ (config default). Both are directories of
per-company CSV shards. Running either side without syncing re-emails roles the
other side already saw, so this script wraps a scan with a pull / union-merge /
push cycle:

    1. refuse to run unless the checkout is on the `data` branch
    2. fetch + hard-reset to origin/data (the cloud amends + force-pushes its
       ledger commit, so origin/data routinely rewrites history and a
       fast-forward would fail)
    3. union-merge local_data/ <-> cloud_data/ (CsvStore.absorb) into BOTH dirs,
       so the scan knows every role the cloud has seen or emailed
    4. run the normal scan (jobscout main, same as run.py)
    5. merge again and commit + push both dirs; if the cloud pushed while we
       were scanning, re-merge and retry the push once

NOT merge_seen_jobs.py: that script is a one-off manual fold in ONE direction
(default cloud_data -> local_data) that rewrites only the destination — no git
sync, no scan, and the dirs are not kept identical as step 3 requires.
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
from jobscout.store import union_merge

ROOT = Path(__file__).resolve().parent
BRANCH = "data"
CLOUD_DIR = ROOT / "cloud_data"  # scan.yml's LEDGER_DIR, at the data-branch root

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
                         f"(currently on {branch!r}); the ledger shards only live there")


def _ledger_dirs(settings: Settings) -> list[Path]:
    """The local shard dir first, then the cloud dir (deduped when they coincide)."""
    local = ROOT / settings.ledger_dir
    return [local] if local == CLOUD_DIR else [local, CLOUD_DIR]


def _mirror_dir(src: Path, dst: Path) -> None:
    """Make dst's shard set byte-identical to src's (copy all, delete strays)."""
    dst.mkdir(parents=True, exist_ok=True)
    for stale in dst.glob("*.csv"):
        if not (src / stale.name).exists():
            stale.unlink()
    for shard in src.glob("*.csv"):
        shutil.copyfile(shard, dst / shard.name)


def _merge_ledgers(settings: Settings, extra: list[Path] = ()) -> None:
    """union_merge dedupes by job_key/canonical URL and keeps emailed=true on merge,
    so this is idempotent regardless of which dir is treated as primary."""
    dirs = _ledger_dirs(settings)
    union_merge(dirs[0], settings.track_names, extra_dirs=dirs[1:], extra_files=extra)
    for d in dirs[1:]:
        _mirror_dir(dirs[0], d)


def _sync_with_remote(settings: Settings) -> None:
    """Sync to origin/data without losing local ledger rows: snapshot the shard
    dirs, hard-reset to the remote tip, then fold the snapshots back in.

    reset --hard, not a fast-forward merge: scan.yml's amend+force-push means
    origin/data is often not a descendant of the previous tip. Local-only
    commits on `data` are discarded by the reset; their shard rows survive
    only via the snapshot/union-merge done here."""
    _git("fetch", "origin", BRANCH)
    tmp = Path(tempfile.mkdtemp(prefix="jobscout_ledger_"))
    snapshots: list[Path] = []
    for i, d in enumerate(_ledger_dirs(settings)):
        if d.is_dir():
            snap_dir = tmp / f"dir_{i}"
            shutil.copytree(d, snap_dir)
            snapshots.extend(sorted(snap_dir.glob("*.csv")))
            shutil.rmtree(d)  # drop untracked strays; the reset restores tracked shards
    _git("reset", "--hard", f"origin/{BRANCH}")
    _merge_ledgers(settings, extra=snapshots)
    shutil.rmtree(tmp, ignore_errors=True)


def _commit_and_push(settings: Settings) -> None:
    for attempt in (1, 2):
        paths = [d.relative_to(ROOT).as_posix() for d in _ledger_dirs(settings)]
        if not _git_out("status", "--porcelain", "--", *paths):
            log.info("ledger unchanged; nothing to push")
            return
        # add first: new per-company shards are untracked, and a pathspec commit
        # only picks up files git already knows about
        _git("add", "-A", "--", *paths)
        # pathspec commit: only the ledger, never other staged/dirty files
        _git("commit", "-m", "update job ledger (local run)", "--", *paths)
        if _git("push", "origin", BRANCH, check=False).returncode == 0:
            log.info("ledger pushed to origin/%s", BRANCH)
            return
        if attempt == 1:
            # cloud push raced ours and won: _sync_with_remote snapshots our
            # just-committed rows from the working tree, discards our commit
            # via hard-reset, then union-merges the rows back for the retry
            log.warning("push rejected; merging remote changes and retrying")
            _sync_with_remote(settings)
    raise SystemExit("push failed twice; resolve manually (git pull --rebase, "
                     "then re-run local_run.py)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _ensure_data_branch()
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")  # before Settings.load so LEDGER_DIR etc. apply
    settings = Settings.load(ROOT)

    _sync_with_remote(settings)  # pre-scan: learn what the cloud already saw/emailed

    from jobscout.__main__ import main as run_scan  # same entry as run.py
    run_scan()  # on failure the ledger is unsaved; propagate and skip the push

    _sync_with_remote(settings)  # post-scan: fold in anything the cloud pushed meanwhile
    _commit_and_push(settings)


if __name__ == "__main__":
    sys.exit(main())
