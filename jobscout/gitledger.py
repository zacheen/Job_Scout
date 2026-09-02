"""git plumbing for the ledger shard dirs, shared by local_run.py and merge_seen_jobs.py.

Both scripts move the same per-company CSV shards between the local checkout
and origin/data, and both have to survive the cloud (scan.yml) amending and
force-pushing that branch underneath them. The safe sequence is

    acquire_single_instance_lock -> ensure_branch -> ensure_reset_safe
        -> sync_with_remote -> commit_and_push

and nothing here enforces it, so a caller that pushes must run all five in that
order. The one sanctioned exception is merge_seen_jobs.py --stage-only: it never
resets or pushes, so it skips ensure_reset_safe and sync_with_remote — but it
still takes the lock and still calls ensure_branch, which guards a rule of its
own (ledger shards belong on this branch only, see .gitignore).

Callers pass the shard dirs and a `merge` callback; nothing here knows how rows
are folded together (that is union_merge's job in jobscout.store). `merge` may
be called more than once — commit_and_push re-runs it when the cloud wins the
push race — so a caller reading its result must read it after the push.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

BRANCH = "data"
# Untracked, and shared by every script that resets/pushes the ledger, so they
# exclude each other and not just other copies of themselves.
LOCK_FILENAME = "local_run.lock"
# Untracked record of every sweep_pack_garbage pass that had something to report.
PACK_GARBAGE_LOG = "pack_garbage.txt"
# A repack renames <hash>.pack into place before <hash>.idx, so for a moment a
# live pack is indistinguishable from an orphan. Only sweep packs older than this.
SWEEP_MIN_AGE_SECONDS = 600

# Shard files rescued from a hard-reset, one list per ledger dir, same order as
# the dirs passed in. A dir that did not exist contributes an empty list, so the
# two sequences stay index-aligned.
Snapshots = list[list[Path]]
MergeLedgers = Callable[[Snapshots], None]

log = logging.getLogger("gitledger")
_lock_handle: int | None = None


def acquire_single_instance_lock(root: Path) -> None:
    """Refuse to start while another ledger script is mid-run.

    Two concurrent runs fetch, hard-reset and push the same repo, so besides
    clobbering each other's ledger their git auto-gc passes collide: on Windows
    the losing gc cannot unlink a pack another process holds open, so it deletes
    the .idx, leaves the .pack, and the orphan becomes garbage until
    sweep_pack_garbage collects it (2026-08-18: 59 MB of it, from a debugger run
    and a shell run overlapping).

    The handle is deliberately never closed — it is parked in a module global so
    it outlives this call, and the OS drops the lock when the process exits.
    That makes a crashed run self-healing: no stale lock file to clean up.
    """
    global _lock_handle
    lockfile = root / LOCK_FILENAME
    handle = os.open(lockfile, os.O_RDWR | os.O_CREAT)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)  # byte 0 may sit past EOF; Windows allows that
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        raise SystemExit(
            f"another ledger run already holds {lockfile.name}; refusing to run a "
            "second one (concurrent runs corrupt the ledger and the git object store). "
            "Wait for it to finish, or kill it if it is stuck.")
    _lock_handle = handle


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in `root`, echoing output to the console.

    stdin is closed so an auto-gc that cannot unlink a pack gives up silently
    instead of blocking the run on Windows git's `Should I try again? (y/n)`
    prompt, which it only asks when stdin and stderr are both consoles. The
    orphaned pack it leaves behind is sweep_pack_garbage's job.
    """
    proc = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed (exit {proc.returncode})")
    return proc


def git_out(root: Path, *args: str) -> str:
    """Run git in `root` and return its stdout. Fails the same way git() does —
    stderr is captured here, so it is folded into the SystemExit message."""
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed (exit {proc.returncode}): "
                         f"{proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_branch(root: Path, script: str, branch: str = BRANCH) -> None:
    current = git_out(root, "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        raise SystemExit(f"{script} must run on the {branch!r} branch "
                         f"(currently on {current!r}); the ledger shards only live there")


def ensure_reset_safe(root: Path, ledger_dirs: Sequence[Path], script: str,
                      branch: str = BRANCH) -> None:
    """Refuse to run when sync_with_remote's hard-reset would destroy work:
    uncommitted changes, or commits missing from origin/data, that touch
    anything OUTSIDE the shard dirs (2026-08-05: uncommitted config.yaml and
    audit_dropped.py revisions were silently wiped exactly this way). Shard
    dirs are exempt — their rows survive the reset via snapshot/union-merge.
    Untracked files are exempt — the reset leaves them alone. Pre-flight only:
    edits made after this check are still wiped by the next sync."""
    excludes = [f":(exclude){d.relative_to(root).as_posix()}" for d in ledger_dirs]
    dirty = git_out(root, "status", "--porcelain", "--untracked-files=no",
                    "--", ".", *excludes)
    if dirty:
        raise SystemExit(
            f"uncommitted changes would be wiped by the hard-reset to origin/{branch}:\n"
            f"{dirty}\ncommit or stash them, then re-run {script}")
    git(root, "fetch", "origin", branch)
    unpushed = git_out(root, "log", "--format=%h %s", f"origin/{branch}..HEAD",
                       "--", ".", *excludes)
    if unpushed:
        raise SystemExit(
            f"commits not on origin/{branch} touch files outside the shard dirs and "
            f"would be dropped by the hard-reset:\n{unpushed}\n"
            f"push {branch} (or move the work elsewhere), then re-run {script}")


def sync_with_remote(root: Path, ledger_dirs: Sequence[Path], merge: MergeLedgers,
                     branch: str = BRANCH) -> None:
    """Sync to origin/<branch> without losing local ledger rows: snapshot the
    shard dirs, hard-reset to the remote tip, then fold the snapshots back in
    via `merge`.

    reset --hard, not a fast-forward merge: scan.yml's amend+force-push means
    origin/data is often not a descendant of the previous tip. Local-only
    commits on the branch are discarded by the reset; their shard rows survive
    only via the snapshot/merge done here."""
    git(root, "fetch", "origin", branch)
    tmp = Path(tempfile.mkdtemp(prefix="jobscout_ledger_"))
    try:
        snapshots: Snapshots = []
        for i, d in enumerate(ledger_dirs):
            if d.is_dir():
                snap_dir = tmp / f"dir_{i}"
                shutil.copytree(d, snap_dir)
                snapshots.append(sorted(snap_dir.glob("*.csv")))
                shutil.rmtree(d)  # drop untracked strays; the reset restores tracked shards
            else:
                snapshots.append([])
        git(root, "reset", "--hard", f"origin/{branch}")
        merge(snapshots)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def commit_and_push(root: Path, ledger_dirs: Sequence[Path], message: str,
                    merge: MergeLedgers, branch: str = BRANCH) -> None:
    """Commit the shard dirs and push, re-merging once if the cloud got there first."""
    paths = [d.relative_to(root).as_posix() for d in ledger_dirs]
    for attempt in (1, 2):
        if not git_out(root, "status", "--porcelain", "--", *paths):
            log.info("ledger unchanged; nothing to push")
            return
        staged = _matchable(root, paths)
        # add first: new per-company shards are untracked, and a pathspec commit
        # only picks up files git already knows about
        git(root, "add", "-A", "--", *staged)
        # pathspec commit: only the ledger, never other staged/dirty files
        git(root, "commit", "-m", message, "--", *staged)
        if git(root, "push", "origin", branch, check=False).returncode == 0:
            log.info("ledger pushed to origin/%s", branch)
            return
        if attempt == 1:
            # cloud push raced ours and won: sync_with_remote snapshots our
            # just-committed rows from the working tree, discards our commit
            # via hard-reset, then merges the rows back for the retry
            log.warning("push rejected; merging remote changes and retrying")
            sync_with_remote(root, ledger_dirs, merge, branch=branch)
    raise SystemExit("push failed twice; resolve manually (git pull --rebase, "
                     "then re-run)")


def sweep_pack_garbage(root: Path, min_age_seconds: float = SWEEP_MIN_AGE_SECONDS) -> None:
    """Delete packs a git gc could not unlink, and append what it found to PACK_GARBAGE_LOG.

    Windows refuses to unlink a file another process holds open without
    FILE_SHARE_DELETE, which GitKraken and AV scanners do not pass. gc deletes
    the .idx before the .pack, so the survivor is an .idx-less .pack that git can
    no longer read and never revisits — gc only ever tries to unlink the pack it
    just replaced, not older garbage — so it accumulates until swept (2026-09-02:
    275 MiB of it). Call this after the run's last git command, under the
    single-instance lock, so no ledger run of ours is mid-repack.

    Nothing here can fail the run: locked files are logged and retried next
    sweep. The log matters because git() leaves git no console to report the
    failed unlink on, making this the only trace that it happened.
    """
    pack_dir = root / ".git" / "objects" / "pack"
    if not pack_dir.is_dir():
        return

    now = time.time()
    freed = 0
    removed: list[str] = []
    left: list[str] = []
    for pack in sorted(pack_dir.glob("*.pack")):
        base = pack.with_suffix("")
        # .keep marks a pack an in-flight fetch is still assembling: its .idx is
        # missing for the opposite reason an orphan's is, and gc honours it too.
        if base.with_suffix(".idx").exists() or base.with_suffix(".keep").exists():
            continue
        try:
            stat = pack.stat()
        except OSError:
            continue  # vanished under us
        age = now - stat.st_mtime
        if age < min_age_seconds:
            left.append(f"  too new  {pack.name} ({age:.0f}s old, {_mib(stat.st_size)})")
            continue
        try:
            pack.unlink()
        except OSError as err:
            left.append(f"  locked   {pack.name} ({_mib(stat.st_size)}): {err.strerror}")
            continue
        freed += stat.st_size
        removed.append(f"  removed  {pack.name} ({_mib(stat.st_size)})")
        for suffix in (".rev", ".mtimes", ".bitmap", ".promisor"):
            sibling = base.with_suffix(suffix)
            try:
                sibling.unlink(missing_ok=True)
            except OSError as err:
                left.append(f"  locked   {sibling.name}: {err.strerror}")

    if not removed and not left:
        return
    header = (f"{datetime.now():%Y-%m-%d %H:%M:%S} reclaimed {_mib(freed)} from "
              f"{len(removed)} pack(s), {len(left)} file(s) left behind")
    with (root / PACK_GARBAGE_LOG).open("a", encoding="utf-8") as fh:
        fh.write("\n".join([header, *removed, *left]) + "\n")
    log.info("pack garbage: reclaimed %s, %d file(s) left behind (see %s)",
             _mib(freed), len(left), PACK_GARBAGE_LOG)


def _mib(size: int) -> str:
    return f"{size / (1 << 20):.1f} MiB"


def _matchable(root: Path, paths: Sequence[str]) -> list[str]:
    """Drop pathspecs git would reject. A dir that is neither on disk nor in the
    index aborts `git add`/`git commit` with "did not match any files" — unlike
    `git status`, which tolerates it (merge_seen_jobs.py lists the cloud dir
    even on a checkout that never had one)."""
    kept = [p for p in paths if (root / p).exists() or git_out(root, "ls-files", "--", p)]
    if not kept:
        # Never fall through to a bare `git add -A`, which would stage the repo.
        raise SystemExit(f"no ledger dir to commit among {list(paths)}")
    return kept
