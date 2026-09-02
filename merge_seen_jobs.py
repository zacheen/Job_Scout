"""One-shot manual merge: fold one ledger shard directory into the other.

Dedup/merge logic lives in CsvStore (absorb + merge_rows) via union_merge; this
script only wires the directories together.

Direction is selectable: --to local (default) folds cloud_data/ into the local
shard dir (config ledger_dir) — the manual "pull what the cloud saw" direction,
and the ONLY way cloud rows ever enter local_data/ (local_run.py deliberately
never merges that way). --to cloud reverses it (rarely needed: local_run.py
already folds local -> cloud on every run). Rewrites the destination; the source
dir is kept by default, --delete-source removes it. WARNING: the deleted source
may be the ledger the next cloud/local run reads, which would make that run
treat the whole ledger as unseeded.

The result is committed and pushed to origin/data the same way local_run.py
pushes its scan (jobscout/gitledger.py): take the shared single-instance lock,
pre-flight the hard-reset safety check, sync to the remote tip, fold, then
commit + push with one re-merge retry if the cloud force-pushed in between. Use
--stage-only to skip the git side and just stage the merge for a manual commit;
that mode prepares the commit message as a commit.template, which GitKraken
auto-loads into its message box, and installs a one-shot post-commit hook that
clears the template once the commit fires. (CLI users: `git commit` then opens
the editor pre-filled — git insists the template be edited, so tweak it or
use -m.)

--to local also stamps the digest checkpoint file (see jobscout/config.py):
local_run.py's email footer uses it as the "safe to delete" window's lower
bound, since the folded-in cloud roles will no longer re-surface in local
digests.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from jobscout import gitledger
from jobscout.config import DIGEST_CHECKPOINT_FILENAME, DIGEST_TZ, Settings
from jobscout.store import CsvStore, union_merge

ROOT = Path(__file__).resolve().parent
SCRIPT = Path(__file__).name
CLOUD_DIR = ROOT / "cloud_data"
CHECKPOINT = ROOT / DIGEST_CHECKPOINT_FILENAME


# Marks our one-shot cleanup hook so a foreign post-commit hook is never touched.
_HOOK_MARKER = "installed by merge_seen_jobs.py"
_CLEANUP_HOOK = f"""#!/bin/sh
# {_HOOK_MARKER}: one-shot cleanup — drop the prepared ledger commit template
# after the next commit (any commit), then remove this hook.
git config --unset commit.template 2>/dev/null || true
rm -f "$(git rev-parse --git-dir)/gkcommittemplate.txt"
rm -f -- "$0"
"""


def _fold(dest: Path, source: Path, settings: Settings, delete_source: bool,
          snapshots: gitledger.Snapshots) -> CsvStore:
    """Fold `source`'s shards into `dest` and save `dest` back. Returns dest's store.

    `snapshots` is [dest_snap, source_snap] from gitledger.sync_with_remote's
    hard-reset. Each dir re-absorbs its own, which is the ONLY case where
    `source` is rewritten — with both empty (--stage-only) it stays
    byte-identical. Deleting the source belongs here, not main(), because a
    rejected push re-runs this after the reset has restored it.
    """
    dest_snap, source_snap = snapshots
    if source_snap and source.is_dir():
        union_merge(source, settings.track_names, extra_files=source_snap)
    if not source.is_dir():
        print(f"{source} not found; rewriting {dest} from its own shards only")
    store = union_merge(dest, settings.track_names,
                        extra_dirs=[source] if source.is_dir() else [],
                        extra_files=dest_snap)
    if delete_source and source.is_dir():
        shutil.rmtree(source)
        print(f"deleted {source}")
    return store


def _stage(dest: Path, source: Path, source_deleted: bool, message: str) -> None:
    """git add the merge result and pre-fill the next commit's message.

    commit.template -> .git/gkcommittemplate.txt is what GitKraken reads to
    auto-populate its message box (it ignores MERGE_MSG outside merge states,
    and MERGE_MSG can't coexist with the template anyway: a message equal to
    the template makes git abort with "you did not edit the message"). The
    one-shot post-commit hook unsets the template again, so later unrelated
    commits don't keep inheriting the ledger message.
    """
    gitledger.git(ROOT, "add", "-A", "--", dest.relative_to(ROOT).as_posix())
    if source_deleted:
        # check=False: nothing to stage when the deleted source was never tracked.
        gitledger.git(ROOT, "add", "-A", "--", source.relative_to(ROOT).as_posix(),
                      check=False)
    git_dir = Path(gitledger.git_out(ROOT, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = ROOT / git_dir
    template = git_dir / "gkcommittemplate.txt"
    template.write_text(message + "\n", encoding="utf-8", newline="\n")
    gitledger.git(ROOT, "config", "commit.template", str(template))
    hook = git_dir / "hooks" / "post-commit"
    if hook.exists() and _HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
        print(f"NOT installing cleanup hook ({hook} already exists); after committing, "
              "run: git config --unset commit.template")
    else:
        # LF endings are mandatory: sh chokes on CRLF hook scripts.
        hook.write_text(_CLEANUP_HOOK, encoding="utf-8", newline="\n")
    print(f"staged {dest.name}/; commit message prepared: {message!r}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Fold one ledger shard directory into the other.")
    parser.add_argument(
        "--to", choices=("local", "cloud"), default="local",
        help="merge destination: 'local' folds cloud_data/ into the config ledger_dir "
             "(default); 'cloud' folds the config ledger_dir into cloud_data/")
    parser.add_argument(
        "--delete-source", action="store_true",
        help="delete the source dir after a successful merge (kept by default); "
             "beware: the next run reading it would then re-seed from scratch")
    parser.add_argument(
        "--stage-only", action="store_true",
        help="stage the merge for a manual commit instead of committing and pushing "
             "it (prepares the commit message for GitKraken)")
    args = parser.parse_args()

    # Before any ledger write, --stage-only included: a local_run.py scan
    # hard-resets these same dirs and would wipe an unlocked fold's work.
    gitledger.acquire_single_instance_lock(ROOT)
    settings = Settings.load(ROOT)
    local_dir = ROOT / settings.ledger_dir
    if local_dir == CLOUD_DIR:
        print(f"ledger_dir already points at {CLOUD_DIR}; nothing to merge")
        return 0
    dest, source = (local_dir, CLOUD_DIR) if args.to == "local" else (CLOUD_DIR, local_dir)
    message = ("update from cloud to local" if args.to == "local"
               else "update from local to cloud")

    # [dest, source] order: matches _fold's snapshot slots, and doubles as the
    # commit pathspec (source must be listed for --delete-source to get staged).
    ledger_dirs = [dest, source]
    store: CsvStore | None = None

    def fold(snapshots: gitledger.Snapshots) -> None:
        nonlocal store
        store = _fold(dest, source, settings, args.delete_source, snapshots)

    source_existed = source.is_dir()
    # Even --stage-only needs the branch check: it stages the shard dirs and
    # arms a commit message, so on main it would tee up a ledger commit there.
    gitledger.ensure_branch(ROOT, SCRIPT)
    if args.stage_only:
        fold([[], []])  # no reset happened, so there is nothing to rescue
    else:
        gitledger.ensure_reset_safe(ROOT, ledger_dirs, SCRIPT)
        # Fold on top of the remote tip so the commit below can fast-forward.
        gitledger.sync_with_remote(ROOT, ledger_dirs, fold)

    if args.to == "local":
        # A fold makes local_data know every role the cloud emailed so far, so
        # those digests never re-surface in local digests. now() is >= the
        # folded cloud_data's actual freshness (its last sync), so a borderline
        # digest gets flagged "review before deleting", never a false "safe".
        CHECKPOINT.write_text(datetime.now(DIGEST_TZ).isoformat(timespec="seconds") + "\n",
                              encoding="utf-8")
        print(f"stamped {CHECKPOINT.name}: cloud digests sent before now will be "
              "flagged 'review before deleting' in future local digest footers")

    if args.stage_only:
        _stage(dest, source, args.delete_source and source_existed, message)
    else:
        gitledger.commit_and_push(ROOT, ledger_dirs, message, fold)
    # After the last git call of either mode, still holding the lock, so an
    # .idx-less .pack here is garbage and not a pack being renamed into place.
    # --stage-only sweeps too: the garbage outlives whichever run created it.
    gitledger.sweep_pack_garbage(ROOT)
    # Last, not right after the fold: a rejected push re-runs fold, and the
    # counts have to describe the ledger that actually reached the remote.
    assert store is not None  # fold() ran on every path above
    print(f"wrote {dest}: {len(store)} rows, {len(store.known_uids())} source uids, "
          f"{len(store.known_urls())} urls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
