"""Fixed entry point for the scheduled daily run.

    conda run --no-capture-output -n ML python scheduled_check.py run
        parks the GitKraken hand-off, stashes tracked changes outside the shard
        dirs and switches to `data` when needed, runs local_run.py (SENDS MAIL,
        hard-resets the checkout to origin/data, pushes the ledger) with its
        output in a fixed log, attempts to commit leftover shard rows, switches
        back, pops the stash and restores the hand-off, then prints the exit
        code and a summary of that log.

The scheduled task runs under an allowlist that matches command strings
verbatim, and a prompt nobody answers stalls the run. The summary is printed
here so the check never has to search the log with a command of its own, and
the log sits at a fixed path because no allowlist rule could name a
per-session scratchpad.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from jobscout.config import Settings
from jobscout.gitledger import BRANCH, LOCK_FILENAME

ROOT = Path(__file__).resolve().parent
# The ignore_* rule in .gitignore keeps it untracked, and since no branch tracks
# the name, the hard-reset to origin/data leaves it alone.
LOG = ROOT / "ignore_scheduled_run.log"
STASH_MESSAGE = "scheduled_check auto-stash"
# Present in the git dir while an operation is half done. Stashing or switching
# then would split the user's merge or rebase across two places.
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
               "rebase-merge", "rebase-apply", "sequencer")

# Keep these patterns aligned with emitted messages or INFO milestones can disappear.
MILESTONES = re.compile(
    r"fetched=\d|first run: seeded|no new roles|nothing left to score|scorable \(of"
    r"|emailed \d+ roles|emailable \(of|all already in the ledger|suppressed \d+ role"
    r"|seeding \d+ new source|scoring \d+ roles across|jd retry:|keyword fallback active"
    r"|ledger pushed|ledger unchanged|pack garbage:")
LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (\w+) ")
# Known_concern.md #4 accepts this volume, so only its count is shown.
TRACK_CONFLICT = "track conflict for "
# Count empty boards because widespread emptiness can signal a fetch-wide failure.
EMPTY_BOARD = "board lists no openings this run"
# git's checkout progress. Each hard reset rewrites every shard file and prints many
# of these, which buried the warnings. The "HEAD is now at" line after it stays.
GIT_PROGRESS = "Updating files:"


def _path_key(path: str) -> str:
    # Windows treats Notes.txt and notes.txt as one file.
    return path.casefold() if os.name == "nt" else path


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _shard_dirs() -> list[str]:
    """The shard dirs, which ensure_reset_safe exempts because local_run
    snapshots and union-merges their rows across the hard-reset. Stashing them
    would make the pop conflict with the rows the scan writes."""
    import local_run
    if local_run.load_dotenv is not None:
        local_run.load_dotenv(ROOT / ".env")  # LEDGER_DIR may live there
    return [d.relative_to(ROOT).as_posix()
            for d in local_run._ledger_dirs(Settings.load(ROOT))]


class HandoffParking:
    """Moves the GitKraken hand-off out of the commit-msg guard's sight while
    the scan runs, and back afterwards.

    The hand-off is the gkcommittemplate.* files plus commit.template, armed for
    a staged change on whatever branch the user left. While they exist the
    guard checks every commit on every branch. It refuses local_run's ledger
    commit, whose fixed message repeats the previous ledger commit, and with
    any other message it would retire the user's prepared one. With them gone
    it exits before checking anything."""

    PARKED = "scheduled_check_parked"
    CONFIG_RECORD = "local-config-commit.template"  # absent when the config was unset

    def __init__(self, git_dir: Path, git) -> None:
        self._git = git
        self._git_dir = git_dir
        self.where = git_dir / self.PARKED
        self.parked = False

    def blocked(self) -> str | None:
        if self.where.exists():
            return (f"an earlier run left the GitKraken hand-off parked in {self.where}. "
                    "Once the checkout and stash are back, move its gkcommittemplate.* "
                    "files into the git dir without overwriting any, set "
                    f"`git config --local commit.template` to the contents of "
                    f"{self.CONFIG_RECORD} if it is there, then delete the folder")
        return None

    def park(self) -> str | None:
        files = sorted(self._git_dir.glob("gkcommittemplate.*"))
        template = self._git("config", "--local", "--get", "commit.template")
        if not files and template.returncode != 0:
            return None
        # The dir is the record. It exists before anything moves, so a run that
        # dies partway leaves it for blocked() to find, holding what did move.
        self.where.mkdir()
        self.parked = True
        if template.returncode == 0:
            (self.where / self.CONFIG_RECORD).write_text(template.stdout.strip(),
                                                         encoding="utf-8")
            if self._git("config", "--local", "--unset", "commit.template").returncode != 0:
                # Nothing has moved yet, so undo the record rather than leave a
                # folder that unpark reads as a clash and blocked() then refuses.
                (self.where / self.CONFIG_RECORD).unlink()
                self.where.rmdir()
                self.parked = False
                return "could not unset commit.template"
        for f in files:
            f.rename(self.where / f.name)
        print(f"parked the GitKraken hand-off in {self.where}")
        return None

    def unpark(self) -> bool:
        if not self.parked:
            return True
        record = self.where / self.CONFIG_RECORD
        returning = [f for f in self.where.iterdir() if f != record]
        clashes = [f.name for f in returning if (self._git_dir / f.name).exists()]
        if (record.exists() and self._git("config", "--local", "--get",
                                          "commit.template").returncode == 0):
            clashes.append("commit.template")
        if clashes:
            print(f"HANDOFF STILL PARKED in {self.where}: a new hand-off was armed "
                  f"during the run ({', '.join(clashes)}), so both are kept")
            return False
        for f in returning:
            f.rename(self._git_dir / f.name)
        if record.exists():
            proc = self._git("config", "--local", "commit.template",
                             record.read_text(encoding="utf-8"))
            if proc.returncode != 0:
                print(f"HANDOFF STILL PARKED in {self.where}: could not restore "
                      f"commit.template, its value is kept there. {proc.stderr.strip()}")
                return False
            record.unlink()
        self.where.rmdir()
        self.parked = False
        print("put the GitKraken hand-off back")
        return True


class CheckoutGuard:
    """Puts the checkout on `data` with nothing local_run.py would refuse, and
    afterwards puts back the branch and the changes it found."""

    RESCUE_REF = "refs/scheduled-check/rescue/"

    def __init__(self, root: Path, shard_dirs: list[str]) -> None:
        self._root = root
        self._shard_dirs = shard_dirs
        self._excludes = [f":(exclude){d}" for d in shard_dirs]
        self._original: str | None = None  # set only once the switch succeeded
        self._stash: str | None = None
        self._stash_base: str | None = None
        self._stash_push_failed = False
        self._ledger_fd: int | None = None
        proc = self._git("rev-parse", "--absolute-git-dir")
        if proc.returncode != 0:
            raise SystemExit(f"not a git checkout: {root}\n{proc.stderr.strip()}")
        self._git_dir = Path(proc.stdout.strip())
        self._handoff = HandoffParking(self._git_dir, self._git)

    def prepare(self) -> str | None:
        """Returns why local_run.py must not start, or None when it may."""
        print("=== CHECKOUT PREP ===")
        # The lock before the parked check, so a second invocation meets the
        # running one rather than reading its parked hand-off as abandoned.
        blocked = (self._operation_in_progress() or self._acquire_ledger_lock()
                   or self._handoff.blocked() or self._handoff.park())
        if blocked:
            print(f"left the checkout alone: {blocked}")
            return blocked
        branch = self._out("symbolic-ref", "-q", "--short", "HEAD")
        original = branch or self._out("rev-parse", "HEAD")
        print(f"starting on: {original}")
        # local_run's hard-reset keeps shard rows but not their staged state, and
        # a pathspec-limited stash still records the whole index.
        staged = self._paths("diff", "--cached")
        outside = self._paths("diff", "--cached", "--", ".", *self._excludes)
        if staged is None or outside is None:
            return "could not list the staged changes"
        if staged - outside:
            return ("staged shard changes would lose their staged state: "
                    + ", ".join(sorted(staged - outside)))
        dirty = self._out("status", "--porcelain", "--untracked-files=no",
                          "--", ".", *self._excludes)
        if dirty:
            reason = self._stash_changes(dirty)
            if reason:
                return reason
        if branch != BRANCH:
            proc = self._git("checkout", "--no-overwrite-ignore", BRANCH)
            if proc.returncode != 0:
                print(proc.stderr.strip())
                return f"git checkout {BRANCH} failed"
            self._original = original
            print(f"switched to {BRANCH}")
        fetch = self._git("fetch", "origin", BRANCH)
        if fetch.returncode != 0:
            print(fetch.stderr.strip())
            return f"git fetch origin {BRANCH} failed"
        collisions = self._reset_collisions()
        if collisions:
            return collisions
        # The child loads its code and config before its own hard-reset, so
        # origin/data's versions reach that load only if the tip moves now.
        ff = self._git("merge", "--ff-only", "-q", f"origin/{BRANCH}")
        print(f"fast-forwarded {BRANCH} to origin/{BRANCH}" if ff.returncode == 0 else
              f"could not fast-forward {BRANCH}, the scan starts from the local tip: "
              + ff.stderr.strip())
        return None

    def restore(self, after_scan: bool = False) -> bool:
        """Returns whether the checkout and the hand-off are back where
        prepare() found them. `after_scan` allows committing the shard rows a
        failed scan left."""
        try:
            print("=== CHECKOUT RESTORE ===")
            if after_scan and not self._rescue_shards():
                return False
            # Parked until everything else is back, since a commit made while
            # restoring would otherwise meet the guard.
            return self._restore_checkout() and self._handoff.unpark()
        finally:
            if self._ledger_fd is not None:
                os.close(self._ledger_fd)  # closing drops the lock
                self._ledger_fd = None

    def _rescue_shards(self) -> bool:
        """Commits the shard rows a scan left uncommitted, so the checkout can
        leave `data` and the next prep is not refused for staged shards. The
        next local_run keeps those rows through its snapshot even though its
        hard-reset drops the commit, and the ref keeps the commit reachable.
        Returns False only when the rows are there but could not be kept."""
        if self._out("symbolic-ref", "-q", "--short", "HEAD") != BRANCH:
            return True
        staged = self._paths("diff", "--cached")
        unstaged = self._paths("diff")
        untracked = self._paths_z("ls-files", "--others", "--exclude-standard")
        if staged is None or unstaged is None or untracked is None:
            print("RESTORE FAILED: could not list what the scan left")
            return False
        # Untracked files outside the shard dirs do not belong in the rescue commit.
        untracked = {p for p in untracked
                     if any(p.startswith(f"{d}/") for d in self._shard_dirs)}
        changed = staged | unstaged | untracked
        if not changed:
            return True
        # Only CSVs directly inside a shard dir, the rows the snapshot can absorb.
        rows = {p for p in changed if p.endswith(".csv") and "/" in p
                and p.rsplit("/", 1)[0] in self._shard_dirs}
        if changed - rows or self._operation_in_progress():
            print("left the scan's changes as they are, since they are not all "
                  "shard rows:\n" + "\n".join(sorted(changed - rows)))
            return True
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        message = f"keep the ledger rows a failed scheduled run left ({stamp})"
        # The index first, so a row staged in one version and edited after keeps
        # both versions in history.
        if staged and self._git("commit", "-q", "-m", f"{message}, staged").returncode != 0:
            print("RESTORE FAILED: could not commit the staged shard rows")
            return False
        remaining = unstaged | untracked
        if remaining:
            proc = self._git("add", "-A", "--", *(f":(literal){p}" for p in sorted(remaining)))
            if proc.returncode != 0:
                print(f"RESTORE FAILED: could not stage the shard rows. {proc.stderr.strip()}")
                return False
        pending = self._paths("diff", "--cached")
        if pending is None:
            print("RESTORE FAILED: could not check the staged shard rows")
            return False
        if pending and self._git("commit", "-q", "-m", message).returncode != 0:
            print("RESTORE FAILED: could not commit the shard rows")
            return False
        head = self._out("rev-parse", "HEAD")
        ref = self.RESCUE_REF + stamp
        proc = self._git("update-ref", ref, head)
        if proc.returncode != 0:
            print(f"RESTORE FAILED: rescue commit {head} is on {BRANCH} but {ref} "
                  f"could not be kept. {proc.stderr.strip()}")
            return False
        print(f"RESCUED the scan's ledger rows into {head} on {BRANCH}, kept at {ref}. "
              "The rows remain available for recovery if a later run also fails.")
        return True

    def _restore_checkout(self) -> bool:
        if self._original is not None:
            proc = self._git("checkout", "--no-overwrite-ignore", self._original)
            if proc.returncode != 0:
                print(proc.stderr.strip())
                kept = f", stash {self._stash} kept" if self._stash else ""
                print(f"RESTORE FAILED: still on {BRANCH}, could not check out "
                      f"{self._original}{kept}")
                return False
            print(f"switched back to {self._original}")
            self._original = None
        elif self._stash is None:
            print("nothing to restore")
        if self._stash is not None:
            if not self._pop():
                return False
            self._stash = None
        return True

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        # stdin closed for the reason gitledger.git gives, an auto-gc prompt.
        return subprocess.run(["git", "-C", str(self._root), *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def _out(self, *args: str) -> str:
        return self._git(*args).stdout.strip()

    def _paths(self, *args: str) -> set[str] | None:
        """Paths a `git diff`-style command names, or None if it failed. -z
        because git quotes non-ASCII names otherwise, and a quoted name used
        as a pathspec matches nothing."""
        cmd, rest = args[0], args[1:]
        proc = self._git(cmd, "--name-only", "--no-renames", "-z", *rest)
        if proc.returncode != 0:
            print(proc.stderr.strip())
            return None
        return set(filter(None, proc.stdout.split("\0")))

    def _paths_z(self, *args: str) -> set[str] | None:
        proc = self._git(*args, "-z")
        if proc.returncode != 0:
            print(proc.stderr.strip())
            return None
        return set(filter(None, proc.stdout.split("\0")))

    def _operation_in_progress(self) -> str | None:
        for name in IN_PROGRESS:
            if (self._git_dir / name).exists():
                return f"a git operation is in progress ({name})"
        return None

    def _acquire_ledger_lock(self) -> str | None:
        # Held from before the stash until after the pop, so no other ledger
        # script can reset the checkout in between, and a second invocation
        # stops here before it truncates the log. _scan's child skips it.
        fd = os.open(self._root / LOCK_FILENAME, os.O_RDWR | os.O_CREAT)
        if not _try_lock(fd):
            os.close(fd)
            return f"another ledger script holds {LOCK_FILENAME}"
        self._ledger_fd = fd
        return None

    def _stash_changes(self, dirty: str) -> str | None:
        print(f"tracked changes outside the shard dirs:\n{dirty}")
        before = self._out("rev-parse", "-q", "--verify", "refs/stash")
        proc = self._git("stash", "push", "-m", STASH_MESSAGE, "--", ".", *self._excludes)
        self._stash_push_failed = proc.returncode != 0
        after = self._out("rev-parse", "-q", "--verify", "refs/stash")
        # git writes the stash before it cleans the tree, so a failed push can
        # still have saved one, and restore() needs it to put the changes back.
        if after and after != before:
            self._stash = after
            self._stash_base = self._out("rev-parse", f"{after}^1")
            print(f"stashed as {after}")
        if proc.returncode != 0 or self._stash is None:
            print(proc.stderr.strip())
            return "git stash push failed"
        return None

    def _reset_collisions(self) -> str | None:
        """local_run's hard-reset to origin/data overwrites an untracked or
        ignored file at a path that tip tracks. Shard dirs are left out, since
        local_run snapshots them before the reset."""
        tracked = self._git("ls-tree", "-r", "--name-only", "-z", f"origin/{BRANCH}")
        local = self._git("ls-files", "--others", "-z", "--", ".", *self._excludes)
        if tracked.returncode != 0 or local.returncode != 0:
            print(tracked.stderr.strip(), local.stderr.strip(), sep="\n")
            return "could not list the files the hard-reset would write"
        files = {_path_key(p) for p in tracked.stdout.split("\0") if p}
        dirs = {a for f in files for a in _ancestors(f)}
        hits = sorted(p for p in filter(None, local.stdout.split("\0"))
                      if _path_key(p) in files or _path_key(p) in dirs
                      or any(a in files for a in _ancestors(_path_key(p))))
        if hits:
            return ("the hard-reset would overwrite untracked files: "
                    + ", ".join(hits))
        return None

    def _pop(self) -> bool:
        stash, base = self._stash, self._stash_base
        held_tree = self._paths("diff", f"{stash}^1", stash)
        held_index = self._paths("diff", f"{stash}^1", f"{stash}^2")
        # The hard-reset can move `data` under a stash taken there. Applying onto
        # files the new tip changed would leave conflict markers in the tree.
        tip_moved = self._paths("diff", base, "HEAD")
        if held_tree is None or held_index is None or tip_moved is None:
            print(f"RESTORE FAILED: stash {stash} kept, could not compare it with HEAD")
            return False
        moved_keys = {_path_key(p) for p in tip_moved}
        moved = {p for p in held_tree | held_index if _path_key(p) in moved_keys}
        if moved:
            print(f"RESTORE FAILED: stash {stash} kept, since HEAD changed files it "
                  "holds:\n" + "\n".join(sorted(moved)))
            return False
        if self._stash_push_failed:
            # A cleanup that failed partway, such as on a file removed from the
            # index but kept on disk, can leave the working version staged, and
            # `stash apply --index` onto that index fails. Writing back the
            # stash's own trees restores both exactly.
            proc = self._git("restore", "--source", stash, "--worktree",
                             "--", ".", *self._excludes)
            if proc.returncode == 0:
                proc = self._git("restore", "--source", f"{stash}^2", "--staged",
                                 "--", ".", *self._excludes)
        else:
            proc = self._git("stash", "apply", "--index", stash)
        if proc.returncode != 0:
            print(proc.stdout.strip(), proc.stderr.strip(), sep="\n")
            print(f"RESTORE FAILED: stash {stash} kept, could not restore its changes")
            return False
        print(f"restored the changes from stash {stash}")
        for line in self._out("stash", "list", "--format=%gd %H").splitlines():
            ref, sha = line.split()
            if sha == stash:
                proc = self._git("stash", "drop", ref)
                if proc.returncode == 0:
                    print(f"dropped {ref}")
                else:
                    print(f"{proc.stderr.strip()}\nstash {stash} also kept, drop failed")
                break
        return True


def _summarize(lines: list[str]) -> None:
    conflicts = empty_boards = 0
    print("=== SUMMARY (log line number, then the line) ===")
    for n, line in enumerate(lines, 1):
        m = LEVEL.match(line)
        level = m.group(1) if m else None
        if TRACK_CONFLICT in line:
            conflicts += 1
        elif EMPTY_BOARD in line:
            empty_boards += 1
        elif line.startswith(GIT_PROGRESS):
            continue
        # Keep unstructured diagnostics such as tracebacks and git output visible.
        elif level != "INFO" or MILESTONES.search(line):
            print(f"{n}: {line}")
    print(f"track conflict lines: {conflicts}")
    print(f"'{EMPTY_BOARD}' lines: {empty_boards}")


def _scan(root: Path, log_path: Path) -> int:
    # Match the log's UTF-8 reader and avoid locale-dependent encoding failures.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    # CheckoutGuard already holds local_run.lock for this child, and a second
    # acquire from another process would refuse, so the child skips its own.
    bootstrap = ("import runpy\n"
                 "from jobscout import gitledger\n"
                 "gitledger.acquire_single_instance_lock = lambda root: None\n"
                 f"runpy.run_path({str(root / 'local_run.py')!r}, run_name='__main__')\n")
    with log_path.open("wb") as log:
        return subprocess.call([sys.executable, "-c", bootstrap], cwd=root, env=env,
                               stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT)


def _run() -> int:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    print(f"run start utc: {start.isoformat()}")
    shard_dirs = _shard_dirs()
    guard = CheckoutGuard(ROOT, shard_dirs)
    code = blocked = None
    scanned = False
    try:
        blocked = guard.prepare()
        # The stash and the switch can change config.yaml, and with it the dirs
        # the child will snapshot, which the prep's checks did not cover.
        if blocked is None and set(_shard_dirs()) != set(shard_dirs):
            blocked = "the ledger dirs changed during the checkout prep"
        if blocked is None:
            scanned = True
            code = _scan(ROOT, LOG)
            # Read while the guard still holds the lock, before a second
            # invocation can truncate the log.
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    finally:
        restored = guard.restore(after_scan=scanned)
    if code is None:
        print(f"=== local_run.py not started: {blocked} ===")
        return 3
    end = datetime.now(timezone.utc).replace(microsecond=0)
    print(f"=== local_run.py exit code: {code} ===")
    print(f"run end utc: {end.isoformat()} ({end - start})")
    print(f"full output, {len(lines)} lines: {LOG}")
    _summarize(lines)
    if not restored and code == 0:
        return 4  # the scan worked but the checkout was left changed
    return code


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["run"]:
        return _run()
    print("usage: scheduled_check.py run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
