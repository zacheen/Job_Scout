"""Tests for scheduled_check (CheckoutGuard, HandoffParking, the scan bootstrap and the log summary), run directly with no pytest.

    conda run --no-capture-output -n ML python test_scheduled_check.py

The CheckoutGuard cases build a throwaway repo with a `main` branch and a `data` branch
that alone tracks local_data/, so nothing here touches this checkout.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scheduled_check import CheckoutGuard, HandoffParking, _scan, _summarize, _try_lock

SHARDS = ["local_data"]
failures = 0


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def make_repo() -> Path:
    """A checkout at <tmp>/work whose origin is the bare <tmp>/origin.git."""
    root = Path(tempfile.mkdtemp(prefix="scheduled_check_test_")) / "work"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    # As this repo's .gitignore does, so the guard's lock file is not a change.
    (root / ".git" / "info" / "exclude").write_text("local_run.lock\n", encoding="utf-8")
    write(root, "code.py", "v1\n")
    write(root, "other.py", "o1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "data")
    write(root, "local_data/a.csv", "row1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "ledger")
    git(root, "checkout", "-q", "main")
    origin = root.parent / "origin.git"
    git(root.parent, "clone", "-q", "--bare", str(root), str(origin))
    git(root, "remote", "add", "origin", str(origin))
    git(root, "fetch", "-q", "origin")
    return root


def push_to_origin_data(root: Path, files: dict[str, str]) -> None:
    other = root.parent / "other"
    git(root.parent, "clone", "-q", "-b", "data", str(root.parent / "origin.git"), str(other))
    git(other, "config", "user.email", "test@example.com")
    git(other, "config", "user.name", "test")
    for name, text in files.items():
        write(other, name, text)
        git(other, "add", name)
    git(other, "commit", "-qm", "remote adds files")
    git(other, "push", "-q", "origin", "data")


def branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")


def staged_patch_id(root: Path) -> str:
    diff = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--binary"],
                          capture_output=True).stdout
    fields = subprocess.run(["git", "patch-id", "--stable"], input=diff,
                            capture_output=True).stdout.decode().split()
    return fields[0] if fields else ""  # the patch-id alone, as the hook compares it


def check(name: str, ok: bool) -> None:
    global failures
    print(("PASS " if ok else "FAIL ") + name)
    failures += not ok


def case_main_with_staged_unstaged_and_untracked(root: Path) -> None:
    write(root, "new.py", "n\n")
    git(root, "add", "new.py")
    write(root, "code.py", "staged\n")
    git(root, "add", "code.py")
    write(root, "code.py", "edited\n")
    write(root, "scratch.txt", "s\n")
    patch_id = staged_patch_id(root)
    guard = CheckoutGuard(root, SHARDS)
    check("main: prepare lets the scan start", guard.prepare() is None)
    check("main: switched to data", branch(root) == "data")
    check("main: data is clean", git(root, "status", "--porcelain", "--untracked-files=no") == "")
    write(root, "local_data/a.csv", "row1\nrow2\n")
    git(root, "commit", "-qam", "scan")
    check("main: restore succeeds", guard.restore())
    check("main: back on main", branch(root) == "main")
    check("main: staged files still staged",
          git(root, "diff", "--cached", "--name-only") == "code.py\nnew.py")
    check("main: staged version of a partly staged file kept",
          git(root, "show", ":code.py") == "staged")
    check("main: unstaged edit still unstaged",
          read(root, "code.py") == "edited\n" and git(root, "diff", "--name-only") == "code.py")
    check("main: untracked file kept", (root / "scratch.txt").exists())
    check("main: stash entry dropped", git(root, "stash", "list") == "")
    check("main: staged patch-id unchanged", patch_id != "" and staged_patch_id(root) == patch_id)


def case_data_with_staged_work_after_reset(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    write(root, "new.py", "new staged file\n")
    write(root, "code.py", "staged\n")
    git(root, "add", "code.py", "new.py")
    write(root, "code.py", "v1\n")
    patch_id = staged_patch_id(root)
    staged_diff = git(root, "diff", "--cached", "--binary")
    push_to_origin_data(root, {"other.py": "remote update\n"})
    guard = CheckoutGuard(root, SHARDS)
    try:
        prepared = guard.prepare() is None
        check("data staged: prep fast-forwarded to the remote tip",
              git(root, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/data"))
        if prepared:
            git(root, "reset", "--hard", "origin/data")  # what local_run does
    finally:
        restored = guard.restore()
    check("data staged: prepare succeeds", prepared)
    check("data staged: restore succeeds", restored and branch(root) == "data")
    check("data staged: tip moved", read(root, "other.py") == "remote update\n")
    check("data staged: staged diff unchanged",
          git(root, "diff", "--cached", "--binary") == staged_diff)
    check("data staged: patch-id unchanged", patch_id != "" and staged_patch_id(root) == patch_id)
    check("data staged: unstaged reversal kept",
          git(root, "show", ":code.py") == "staged" and read(root, "code.py") == "v1\n")
    check("data staged: stash dropped", git(root, "stash", "list") == "")


def case_case_insensitive_collision(root: Path) -> None:
    push_to_origin_data(root, {"Notes.txt": "from the remote\n"})
    write(root, "notes.txt", "mine, untracked\n")
    guard = CheckoutGuard(root, SHARDS)
    blocked = "overwrite untracked" in (guard.prepare() or "")
    check("case: blocked on Windows only", blocked == (os.name == "nt"))
    guard.restore()


def case_case_insensitive_stash_collision(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    write(root, "notes.txt", "mine, staged\n")
    git(root, "add", "notes.txt")
    push_to_origin_data(root, {"Notes.txt": "from the remote\n"})
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    restored = guard.restore()
    check("case stash: restore refuses on Windows only", restored == (os.name != "nt"))
    if os.name == "nt":
        check("case stash: stash kept", "scheduled_check auto-stash" in git(root, "stash", "list"))


def case_stash_cleanup_fails_partway(root: Path) -> None:
    write(root, "code.py", "staged\n")
    git(root, "add", "code.py")
    write(root, "code.py", "worktree\n")
    git(root, "rm", "-q", "--cached", "other.py")
    status = git(root, "status", "--porcelain")
    guard = CheckoutGuard(root, SHARDS)
    check("cleanup: prep refuses", "stash push failed" in (guard.prepare() or ""))
    check("cleanup: still on main", branch(root) == "main")
    check("cleanup: restore succeeds", guard.restore())
    check("cleanup: staged version back", git(root, "show", ":code.py") == "staged")
    check("cleanup: status as before", git(root, "status", "--porcelain") == status)
    check("cleanup: removed file still on disk", read(root, "other.py") == "o1\n")
    check("cleanup: stash dropped", git(root, "stash", "list") == "")


def case_data_dirty_with_unrelated_tip_move(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    write(root, "code.py", "dirty\n")
    write(root, "local_data/a.csv", "row1\nlocal\n")
    guard = CheckoutGuard(root, SHARDS)
    check("data: prepare lets the scan start", guard.prepare() is None)
    check("data: stays on data", branch(root) == "data")
    check("data: shard edit left in the tree", read(root, "local_data/a.csv") == "row1\nlocal\n")
    check("data: code edit stashed", read(root, "code.py") == "v1\n")
    write(root, "other.py", "o2\n")
    git(root, "commit", "-qam", "tip moved other.py")
    check("data: restore succeeds", guard.restore())
    check("data: code edit back", read(root, "code.py") == "dirty\n")


def case_data_tip_moved_a_stashed_file(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    write(root, "code.py", "dirty\n")
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    write(root, "code.py", "v2\n")
    git(root, "commit", "-qam", "tip changed code.py")
    check("moved: restore refuses", guard.restore() is False)
    check("moved: stash kept", "scheduled_check auto-stash" in git(root, "stash", "list"))
    check("moved: tree untouched", read(root, "code.py") == "v2\n")


def case_tip_moved_a_non_ascii_file(root: Path) -> None:
    name = "改動.py"
    write(root, name, "v1\n")
    git(root, "add", name)
    git(root, "commit", "-qm", "non-ascii file")
    git(root, "checkout", "-q", "data")
    git(root, "merge", "-q", "main")
    write(root, name, "dirty\n")
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    write(root, name, "v2\n")
    git(root, "commit", "-qam", "tip changed the non-ascii file")
    check("non-ascii: restore refuses", guard.restore() is False)
    check("non-ascii: tree untouched", read(root, name) == "v2\n")


def case_staged_shard_change(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    write(root, "local_data/a.csv", "row1\nstaged\n")
    git(root, "add", "local_data/a.csv")
    write(root, "code.py", "edited\n")
    guard = CheckoutGuard(root, SHARDS)
    check("staged shard: blocked", "staged shard" in (guard.prepare() or ""))
    check("staged shard: nothing touched",
          guard.restore() and read(root, "code.py") == "edited\n"
          and git(root, "diff", "--cached", "--name-only") == "local_data/a.csv")


def case_sequencer_left_behind(root: Path) -> None:
    (Path(git(root, "rev-parse", "--absolute-git-dir")) / "sequencer").mkdir()
    guard = CheckoutGuard(root, SHARDS)
    check("sequencer: blocked", "sequencer" in (guard.prepare() or ""))
    check("sequencer: still on main", branch(root) == "main" and guard.restore())


def case_lock_held_between_prepare_and_restore(root: Path) -> None:
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    fd = os.open(root / "local_run.lock", os.O_RDWR | os.O_CREAT)
    try:
        check("held: another acquire fails mid-run", not _try_lock(fd))
        guard.restore()
        check("held: released after restore", _try_lock(fd))
    finally:
        os.close(fd)


def case_child_skips_the_held_lock(root: Path) -> None:
    write(root, "local_run.py",
          "from pathlib import Path\n"
          "from jobscout import gitledger\n"
          "gitledger.acquire_single_instance_lock(Path.cwd())\n"
          "print('child ran')\n")
    # The child's temporary cwd lacks jobscout; import it from this checkout.
    os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parent)
    fd = os.open(root / "local_run.lock", os.O_RDWR | os.O_CREAT)
    try:
        _try_lock(fd)
        code = _scan(root, root / "child.log")
    finally:
        os.close(fd)
        del os.environ["PYTHONPATH"]
    log = (root / "child.log").read_text(encoding="utf-8", errors="replace")
    check("child: ran under the parent's lock", code == 0 and "child ran" in log)


def case_untracked_file_the_remote_now_tracks(root: Path) -> None:
    push_to_origin_data(root, {"notes.txt": "from the remote\n", "tools/run.py": "x\n"})
    write(root, "notes.txt", "mine, untracked\n")
    write(root, "tools", "a file where the remote has a dir\n")
    write(root, "code.py", "edited\n")
    guard = CheckoutGuard(root, SHARDS)
    reason = guard.prepare() or ""
    check("collision: blocked", "overwrite untracked" in reason)
    check("collision: names both paths", "notes.txt" in reason and "tools" in reason)
    check("collision: restore succeeds", guard.restore() and branch(root) == "main")
    check("collision: files intact",
          read(root, "notes.txt") == "mine, untracked\n" and read(root, "code.py") == "edited\n")


GUARD_HOOK = Path.home() / ".claude" / "hooks" / "gk-commit-msg-guard.sh"
LEDGER_MESSAGE = "update job ledger (local run)"


def git_dir(root: Path) -> Path:
    return Path(git(root, "rev-parse", "--absolute-git-dir"))


def arm_handoff(root: Path) -> None:
    """Stage a change on main and arm a prepared message for it, the way the
    hand-off does, with the real commit-msg guard installed."""
    shutil.copyfile(GUARD_HOOK, git_dir(root) / "hooks" / "commit-msg")
    write(root, "new.py", "n\n")
    git(root, "add", "new.py")
    (git_dir(root) / "gkcommittemplate.txt").write_text("Add new.py\n", encoding="utf-8")
    (git_dir(root) / "gkcommittemplate.change").write_text(staged_patch_id(root) + "\n")
    git(root, "config", "commit.template", str(git_dir(root) / "gkcommittemplate.txt"))


def ledger_commit(root: Path, row: str) -> int:
    write(root, "local_data/a.csv", f"row1\n{row}\n")
    return subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", LEDGER_MESSAGE,
                           "--", "local_data"], capture_output=True).returncode


def case_armed_handoff_survives_a_repeated_ledger_commit(root: Path) -> None:
    if not GUARD_HOOK.exists():
        check(f"handoff: skipped, {GUARD_HOOK} missing", True)
        return
    git(root, "checkout", "-q", "data")
    git(root, "commit", "-q", "--allow-empty", "-m", LEDGER_MESSAGE)
    git(root, "push", "-q", "origin", "data")
    git(root, "checkout", "-q", "main")
    arm_handoff(root)
    patch_id = staged_patch_id(root)
    guard = CheckoutGuard(root, SHARDS)
    check("handoff: prepare lets the scan start", guard.prepare() is None)
    check("handoff: parked", not list(git_dir(root).glob("gkcommittemplate.*")))
    check("handoff: the repeated ledger message commits while parked",
          ledger_commit(root, "scan") == 0)
    check("handoff: restore succeeds", guard.restore(after_scan=True))
    check("handoff: files back", sorted(f.name for f in git_dir(root).glob("gkcommittemplate.*"))
          == ["gkcommittemplate.change", "gkcommittemplate.txt"])
    check("handoff: config back",
          git(root, "config", "commit.template") == str(git_dir(root) / "gkcommittemplate.txt"))
    check("handoff: staged change still matches the prepared message",
          staged_patch_id(root) == patch_id
          and read_git(root, "gkcommittemplate.change").strip() == patch_id)
    check("handoff: parked dir gone", not (git_dir(root) / "scheduled_check_parked").exists())
    proc = subprocess.run(["git", "-C", str(root), "commit", "-q", "-F",
                           str(git_dir(root) / "gkcommittemplate.txt")], capture_output=True)
    check("handoff: the guard accepts the restored prepared message", proc.returncode == 0)


def case_armed_handoff_refuses_the_ledger_commit_unparked(root: Path) -> None:
    if not GUARD_HOOK.exists():
        check(f"control: skipped, {GUARD_HOOK} missing", True)
        return
    git(root, "checkout", "-q", "data")
    git(root, "commit", "-q", "--allow-empty", "-m", LEDGER_MESSAGE)
    git(root, "checkout", "-q", "main")
    arm_handoff(root)
    git(root, "stash", "-q")
    git(root, "checkout", "-q", "data")
    check("control: the guard refuses the same commit when not parked",
          ledger_commit(root, "scan") != 0)


def read_git(root: Path, name: str) -> str:
    return (git_dir(root) / name).read_text(encoding="utf-8")


def case_failed_scan_rows_rescued(root: Path) -> None:
    write(root, "scratch.txt", "the user's, untracked\n")
    write(root, "code.py", "staged\n")
    git(root, "add", "code.py")
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    write(root, "local_data/a.csv", "row1\nstaged row\n")
    git(root, "add", "local_data/a.csv")
    write(root, "local_data/a.csv", "row1\nstaged row\nlater row\n")
    write(root, "local_data/b.csv", "new shard\n")
    check("rescue: restore succeeds", guard.restore(after_scan=True))
    check("rescue: back on main with the staged edit",
          branch(root) == "main" and git(root, "show", ":code.py") == "staged")
    refs = git(root, "for-each-ref", "--format=%(objectname)", "refs/scheduled-check/rescue/")
    check("rescue: ref kept", refs != "")
    check("rescue: both versions of a.csv and the new shard committed",
          git(root, "show", f"{refs}:local_data/a.csv") == "row1\nstaged row\nlater row"
          and git(root, "show", f"{refs}~1:local_data/a.csv") == "row1\nstaged row"
          and git(root, "show", f"{refs}:local_data/b.csv") == "new shard")
    check("rescue: the user's untracked file left out and kept",
          "scratch.txt" not in git(root, "show", "--name-only", "--format=", refs)
          and read(root, "scratch.txt") == "the user's, untracked\n")


def case_non_shard_leftover_not_rescued(root: Path) -> None:
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    write(root, "code.py", "left by the scan\n")
    write(root, "local_data/a.csv", "row1\nscan\n")
    check("no rescue: restore reports the failure", guard.restore(after_scan=True) is False)
    check("no rescue: nothing committed",
          git(root, "for-each-ref", "refs/scheduled-check/rescue/") == ""
          and read(root, "local_data/a.csv") == "row1\nscan\n")


def case_no_rescue_without_a_scan(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    write(root, "local_data/a.csv", "row1\nmine\n")
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    check("no scan: restore succeeds", guard.restore())
    check("no scan: shard edit left uncommitted",
          git(root, "diff", "--name-only") == "local_data/a.csv"
          and git(root, "for-each-ref", "refs/scheduled-check/rescue/") == "")


def case_parked_dir_left_behind(root: Path) -> None:
    (git_dir(root) / "scheduled_check_parked").mkdir()
    guard = CheckoutGuard(root, SHARDS)
    check("left behind: blocked", "parked" in (guard.prepare() or ""))
    check("left behind: kept", guard.restore() and (git_dir(root) / "scheduled_check_parked").exists())


def case_handoff_armed_during_the_run(root: Path) -> None:
    if not GUARD_HOOK.exists():
        check(f"armed meanwhile: skipped, {GUARD_HOOK} missing", True)
        return
    arm_handoff(root)
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    (git_dir(root) / "gkcommittemplate.txt").write_text("Armed meanwhile\n", encoding="utf-8")
    check("armed meanwhile: restore reports it", guard.restore() is False)
    check("armed meanwhile: both kept",
          read_git(root, "gkcommittemplate.txt") == "Armed meanwhile\n"
          and (git_dir(root) / "scheduled_check_parked" / "gkcommittemplate.txt").exists())


def case_handoff_stays_parked_when_the_checkout_cannot_return(root: Path) -> None:
    if not GUARD_HOOK.exists():
        check(f"stays parked: skipped, {GUARD_HOOK} missing", True)
        return
    arm_handoff(root)
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    write(root, "code.py", "left by the scan\n")  # not a shard row, so no rescue
    write(root, "local_data/a.csv", "row1\nscan\n")
    check("stays parked: restore fails", guard.restore(after_scan=True) is False)
    check("stays parked: hand-off still parked",
          (git_dir(root) / "scheduled_check_parked" / "gkcommittemplate.txt").exists()
          and not (git_dir(root) / "gkcommittemplate.txt").exists())


def check_park_undoes_a_failed_unset() -> None:
    def git_that_cannot_unset(*args: str) -> subprocess.CompletedProcess:
        if args[:3] == ("config", "--local", "--get"):
            return subprocess.CompletedProcess(args, 0, stdout="tpl.txt\n", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="locked")
    root = Path(tempfile.mkdtemp(prefix="scheduled_check_test_"))
    try:
        parking = HandoffParking(root, git_that_cannot_unset)
        reason = parking.park()
        check("unset fails: park reports it", reason == "could not unset commit.template")
        check("unset fails: nothing left parked",
              not parking.where.exists() and not parking.parked and parking.blocked() is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_summary_filters_the_log() -> None:
    log = ["2026-10-17 03:00:01,234 INFO fetched=10 candidates=3 new=2",
           "2026-10-17 03:00:01,234 INFO host x done: 3 jobs in 1.0s",
           "2026-10-17 03:00:02,000 WARNING track conflict for a: b vs c; keeping b",
           "2026-10-17 03:00:02,000 INFO Acme: board lists no openings this run",
           "2026-10-17 03:00:03,000 WARNING something odd",
           "Traceback (most recent call last):",
           "2026-10-17 03:00:04,000 INFO ledger pushed to origin/data"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        _summarize(log)
    shown = out.getvalue()
    check("summary: milestones, warnings and tracebacks shown",
          all(f"\n{n}: " in shown for n in (1, 5, 6, 7)))
    check("summary: routine INFO and counted shapes hidden",
          not any(f"\n{n}: " in shown for n in (2, 3, 4)))
    check("summary: counts", "track conflict lines: 1" in shown
          and "'board lists no openings this run' lines: 1" in shown)


def case_merge_in_progress(root: Path) -> None:
    (Path(git(root, "rev-parse", "--absolute-git-dir")) / "MERGE_HEAD").write_text("x")
    write(root, "code.py", "edited\n")
    guard = CheckoutGuard(root, SHARDS)
    check("merge: blocked", "in progress" in (guard.prepare() or ""))
    check("merge: nothing touched",
          branch(root) == "main" and read(root, "code.py") == "edited\n" and guard.restore())


def case_local_run_lock_held(root: Path) -> None:
    fd = os.open(root / "local_run.lock", os.O_RDWR | os.O_CREAT)
    try:
        _try_lock(fd)
        guard = CheckoutGuard(root, SHARDS)
        check("lock: blocked", "local_run.lock" in (guard.prepare() or ""))
        check("lock: still on main", branch(root) == "main")
    finally:
        os.close(fd)


def case_checkout_refused(root: Path) -> None:
    write(root, "code.py", "edited\n")
    write(root, "local_data/a.csv", "untracked, collides with data\n")
    guard = CheckoutGuard(root, SHARDS)
    check("refused: prepare reports the checkout", "checkout" in (guard.prepare() or ""))
    check("refused: still on main", branch(root) == "main")
    check("refused: restore succeeds", guard.restore())
    check("refused: edit back", read(root, "code.py") == "edited\n")


def case_detached_head(root: Path) -> None:
    git(root, "checkout", "-q", "--detach")
    sha = git(root, "rev-parse", "HEAD")
    guard = CheckoutGuard(root, SHARDS)
    check("detached: switched to data", guard.prepare() is None and branch(root) == "data")
    check("detached: back on the same commit",
          guard.restore() and git(root, "rev-parse", "HEAD") == sha and branch(root) == "HEAD")


def case_clean_data(root: Path) -> None:
    git(root, "checkout", "-q", "data")
    guard = CheckoutGuard(root, SHARDS)
    check("clean: nothing done",
          guard.prepare() is None and guard.restore() and git(root, "stash", "list") == "")


def case_restore_twice(root: Path) -> None:
    write(root, "code.py", "edited\n")
    guard = CheckoutGuard(root, SHARDS)
    guard.prepare()
    check("twice: first restore succeeds", guard.restore())
    check("twice: second restore is a no-op",
          guard.restore() and branch(root) == "main" and read(root, "code.py") == "edited\n")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    for case in (case_main_with_staged_unstaged_and_untracked,
                 case_data_with_staged_work_after_reset, case_case_insensitive_collision,
                 case_case_insensitive_stash_collision, case_stash_cleanup_fails_partway,
                 case_data_dirty_with_unrelated_tip_move, case_data_tip_moved_a_stashed_file,
                 case_tip_moved_a_non_ascii_file, case_staged_shard_change,
                 case_sequencer_left_behind, case_lock_held_between_prepare_and_restore,
                 case_child_skips_the_held_lock, case_untracked_file_the_remote_now_tracks,
                 case_merge_in_progress, case_local_run_lock_held, case_checkout_refused,
                 case_detached_head, case_clean_data, case_restore_twice,
                 case_armed_handoff_survives_a_repeated_ledger_commit,
                 case_armed_handoff_refuses_the_ledger_commit_unparked,
                 case_failed_scan_rows_rescued, case_non_shard_leftover_not_rescued,
                 case_no_rescue_without_a_scan, case_parked_dir_left_behind,
                 case_handoff_armed_during_the_run,
                 case_handoff_stays_parked_when_the_checkout_cannot_return):
        root = make_repo()
        try:
            case(root)
        except Exception as exc:
            check(f"{case.__name__} crashed: {exc!r}", False)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)
    check_park_undoes_a_failed_unset()
    check_summary_filters_the_log()
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
