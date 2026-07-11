"""One-shot manual merge: fold one ledger shard directory into the other.

Dedup/merge logic lives in CsvStore (absorb + merge_rows) via union_merge; this
script only wires the directories together.

Direction is selectable: --to local (default) folds cloud_data/ into the local
shard dir (config ledger_dir) — the manual "pull what the cloud saw" direction.
--to cloud reverses it (rarely needed: local_run.py already mirrors
local -> cloud on every run). Rewrites only the destination; the source dir is
kept by default, --delete-source removes it. WARNING: the deleted source may be
the ledger the next cloud/local run reads, which would make that run treat the
whole ledger as unseeded.

The result is STAGED but never committed: a commit message ("update from cloud
to local" / the reverse) is prepared as a commit.template, which GitKraken
auto-loads into its message box; a one-shot post-commit hook clears the
template after the user fires the commit. (CLI users: `git commit` opens the
editor pre-filled — git insists the template be edited, so tweak it or -m.)
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from jobscout.config import Settings
from jobscout.store import union_merge

ROOT = Path(__file__).resolve().parent
CLOUD_DIR = ROOT / "cloud_data"


# Marks our one-shot cleanup hook so a foreign post-commit hook is never touched.
_HOOK_MARKER = "installed by merge_seen_jobs.py"
_CLEANUP_HOOK = f"""#!/bin/sh
# {_HOOK_MARKER}: one-shot cleanup — drop the prepared ledger commit template
# after the next commit (any commit), then remove this hook.
git config --unset commit.template 2>/dev/null || true
rm -f "$(git rev-parse --git-dir)/gkcommittemplate.txt"
rm -f -- "$0"
"""


def _stage(dest: Path, source: Path, source_deleted: bool, message: str) -> None:
    """git add the merge result and pre-fill the next commit's message.

    commit.template -> .git/gkcommittemplate.txt is what GitKraken reads to
    auto-populate its message box (it ignores MERGE_MSG outside merge states,
    and MERGE_MSG can't coexist with the template anyway: a message equal to
    the template makes git abort with "you did not edit the message"). The
    one-shot post-commit hook unsets the template again, so later unrelated
    commits don't keep inheriting the ledger message.
    """
    subprocess.run(["git", "-C", str(ROOT), "add", "-A", "--",
                    dest.relative_to(ROOT).as_posix()], check=True)
    if source_deleted:
        # check=False: nothing to stage when the deleted source was never tracked.
        subprocess.run(["git", "-C", str(ROOT), "add", "-A", "--",
                        source.relative_to(ROOT).as_posix()], check=False)
    git_dir = Path(subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-dir"],
                                  check=True, capture_output=True, text=True).stdout.strip())
    if not git_dir.is_absolute():
        git_dir = ROOT / git_dir
    template = git_dir / "gkcommittemplate.txt"
    template.write_text(message + "\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(ROOT), "config", "commit.template", str(template)],
                   check=True)
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
    args = parser.parse_args()

    settings = Settings.load(ROOT)
    local_dir = ROOT / settings.ledger_dir
    if local_dir == CLOUD_DIR:
        print(f"ledger_dir already points at {CLOUD_DIR}; nothing to merge")
        return 0
    dest, source = (local_dir, CLOUD_DIR) if args.to == "local" else (CLOUD_DIR, local_dir)

    store = union_merge(dest, settings.track_names,
                        extra_dirs=[source] if source.is_dir() else [])
    if not source.is_dir():
        print(f"{source} not found; rewrote {dest} from its own shards only")
    print(f"wrote {dest}: {len(store)} rows, {len(store.known_uids())} source uids, "
          f"{len(store.known_urls())} urls")

    source_deleted = source.is_dir() and args.delete_source
    if source_deleted:
        shutil.rmtree(source)
        print(f"deleted {source}")

    message = ("update from cloud to local" if args.to == "local"
               else "update from local to cloud")
    _stage(dest, source, source_deleted, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
