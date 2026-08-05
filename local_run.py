"""Local wrapper around run.py that keeps the git-hosted ledger in sync.

Cloud runs (scan.yml) keep their ledger in cloud_data/ at the ROOT of the `data`
branch; local runs use local_data/ (config default). Both are directories of
per-company CSV shards. Automatic sync is ONE-WAY, local -> cloud: the cloud
must learn every role a local scan saw or emailed (so it never re-emails them),
but local_data/ never absorbs cloud rows here. Folding cloud_data/ back into
local_data/ is a deliberate manual step — run merge_seen_jobs.py (default
direction) — so roles the cloud already emailed KEEP re-surfacing (and
re-emailing) in local scans until you fold them in.

    1. refuse to run unless the checkout is on the `data` branch and step 2's
       hard-reset cannot destroy work: no uncommitted changes or unpushed
       commits touching files outside the shard dirs (_ensure_reset_safe)
    2. fetch + hard-reset to origin/data (the cloud amends + force-pushes its
       ledger commit, so origin/data routinely rewrites history and a
       fast-forward would fail); each shard dir is snapshotted first and
       re-absorbs its own snapshot after the reset, so no rows are lost
    3. union-merge local_data/ INTO cloud_data/ (never the reverse)
    4. run the normal scan (jobscout main, same as run.py)
    5. merge again and commit + push both dirs; if the cloud pushed while we
       were scanning, re-merge and retry the push once
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; env vars still work without it
    load_dotenv = None

from jobscout.config import DIGEST_CHECKPOINT_FILENAME, DIGEST_TZ, Settings
from jobscout.store import union_merge

ROOT = Path(__file__).resolve().parent
BRANCH = "data"
CLOUD_DIR = ROOT / "cloud_data"  # scan.yml's LEDGER_DIR, at the data-branch root
CHECKPOINT = ROOT / DIGEST_CHECKPOINT_FILENAME  # untracked: survives the hard-resets below

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


def _ensure_reset_safe(settings: Settings) -> None:
    """Refuse to run when _sync_with_remote's hard-reset would destroy work:
    uncommitted changes, or commits missing from origin/data, that touch
    anything OUTSIDE the shard dirs (2026-08-05: uncommitted config.yaml and
    audit_dropped.py revisions were silently wiped exactly this way). Shard
    dirs are exempt — their rows survive the reset via snapshot/union-merge.
    Untracked files are exempt — the reset leaves them alone. Pre-flight only:
    edits made while the scan is running are still wiped by the post-scan sync."""
    excludes = [f":(exclude){d.relative_to(ROOT).as_posix()}"
                for d in _ledger_dirs(settings)]
    dirty = _git_out("status", "--porcelain", "--untracked-files=no",
                     "--", ".", *excludes)
    if dirty:
        raise SystemExit(
            f"uncommitted changes would be wiped by the hard-reset to origin/{BRANCH}:\n"
            f"{dirty}\ncommit or stash them, then re-run local_run.py")
    _git("fetch", "origin", BRANCH)
    unpushed = _git_out("log", "--format=%h %s", f"origin/{BRANCH}..HEAD",
                        "--", ".", *excludes)
    if unpushed:
        raise SystemExit(
            f"commits not on origin/{BRANCH} touch files outside the shard dirs and "
            f"would be dropped by the hard-reset:\n{unpushed}\n"
            f"push {BRANCH} (or move the work elsewhere), then re-run local_run.py")


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
    lower bound. Timestamps render in DIGEST_TZ, same as digest subject lines."""
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
    job_key/canonical URL and keeps emailed=true on merge, so this is idempotent.

    snapshots[i] holds the shard files snapshotted from _ledger_dirs()[i]; a
    missing dir contributes an empty list, keeping the two lists aligned."""
    dirs = _ledger_dirs(settings)
    union_merge(dirs[0], settings.track_names, extra_files=snapshots[0])
    for d, snap in zip(dirs[1:], snapshots[1:]):
        union_merge(d, settings.track_names, extra_dirs=[dirs[0]], extra_files=snap)


def _sync_with_remote(settings: Settings) -> None:
    """Sync to origin/data without losing local ledger rows: snapshot the shard
    dirs, hard-reset to the remote tip, then fold the snapshots back in.

    reset --hard, not a fast-forward merge: scan.yml's amend+force-push means
    origin/data is often not a descendant of the previous tip. Local-only
    commits on `data` are discarded by the reset; their shard rows survive
    only via the snapshot/union-merge done here."""
    _git("fetch", "origin", BRANCH)
    tmp = Path(tempfile.mkdtemp(prefix="jobscout_ledger_"))
    snapshots: list[list[Path]] = []
    for i, d in enumerate(_ledger_dirs(settings)):
        if d.is_dir():
            snap_dir = tmp / f"dir_{i}"
            shutil.copytree(d, snap_dir)
            snapshots.append(sorted(snap_dir.glob("*.csv")))
            shutil.rmtree(d)  # drop untracked strays; the reset restores tracked shards
        else:
            snapshots.append([])
    _git("reset", "--hard", f"origin/{BRANCH}")
    _merge_ledgers(settings, snapshots)
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
    _ensure_reset_safe(settings)

    _sync_with_remote(settings)  # pre-scan: align with origin/data so the final push can fast-forward

    # This scan's start doubles as the checkpoint window's end: any cloud digest
    # sent before it covered roles that were still live then, so this scan
    # independently re-finds and re-emails them — except roles local_data
    # already knew (the window's lower bound, see _read_checkpoint).
    start = datetime.now(DIGEST_TZ)

    from jobscout.__main__ import main as run_scan  # same entry as run.py
    # Only an uncaught exception here skips the push — the handled saved=False
    # case below still pushes, just without advancing the checkpoint.
    saved = run_scan(digest_footer=_digest_footer(_read_checkpoint(), start))
    if saved:
        _write_checkpoint(start)  # ledger saved: the next window starts where this scan began
    else:
        log.warning("digest email failed; ledger unsaved, checkpoint not advanced "
                    "(unemailed roles retry next run)")

    _sync_with_remote(settings)  # post-scan: re-align with the remote tip, fold scan finds into cloud_data
    _commit_and_push(settings)


if __name__ == "__main__":
    sys.exit(main())
