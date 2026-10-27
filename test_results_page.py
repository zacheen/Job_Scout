"""Tests for digest_snapshot and results_gui, run directly with no pytest.

    conda run --no-capture-output -n ML python test_results_page.py

Every case works in a throwaway directory, so the real ignore_run_results/ is untouched.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import results_gui
from jobscout.digest_snapshot import SnapshotNotifier, load_snapshots, snapshot_paths
from jobscout.models import Job, Score, ScoreScale

failures = 0


def check(name: str, ok: bool) -> None:
    global failures
    print(("PASS " if ok else "FAIL ") + name)
    failures += not ok


def job(uid: str, note: str = "") -> Job:
    return Job(job_uid=uid, company="Acme", title=f"Engineer {uid}", location="San Jose, CA",
               url=f"https://example.com/{uid}", description="", note=note)


def digest(*uids: str):
    return [("Intern", [("Software", [(job(u), Score(80, "fits", ScoreScale.LLM))
                                      for u in uids])])]


class FakeEmail:
    def __init__(self, fail: bool = False):
        self.sent = 0
        self.fail = fail

    def send_digest(self, digest, subject=None):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent += 1


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def snapshot_cases(tmp: Path) -> None:
    out = tmp / "snap"
    inner = FakeEmail()
    SnapshotNotifier(inner, out, footer="foot", clock=lambda: at(20)).send_digest(
        digest("a", "b"), subject="[Job Scout] 2 new roles")
    runs = load_snapshots(out)
    check("a sent digest is written once", inner.sent == 1 and len(runs) == 1)
    check("the snapshot keeps digest order and the footer",
          [j["id"] for j in runs[0]["jobs"]] == ["a", "b"] and runs[0]["footer"] == "foot")

    SnapshotNotifier(FakeEmail(), out, clock=lambda: at(21)).send_digest([])
    check("an empty digest writes nothing", len(snapshot_paths(out)) == 1)

    try:
        SnapshotNotifier(FakeEmail(fail=True), out, clock=lambda: at(22)).send_digest(
            digest("c"))
        raised = False
    except RuntimeError:
        raised = True
    check("a failed send raises and writes nothing", raised and len(snapshot_paths(out)) == 1)

    blocked = tmp / "blocked"
    blocked.write_text("a file where the directory should be", encoding="utf-8")
    try:
        SnapshotNotifier(FakeEmail(), blocked, clock=lambda: at(22)).send_digest(digest("d"))
        swallowed = True
    except OSError:
        swallowed = False
    check("a failed snapshot write does not fail the send", swallowed)

    lone = [("Intern", [("Software", [(job("\ud800"), Score(80, "fits", ScoreScale.LLM))])])]
    try:
        SnapshotNotifier(FakeEmail(), tmp / "surrogate", clock=lambda: at(22)).send_digest(lone)
        swallowed = True
    except Exception:
        swallowed = False
    check("an unencodable field does not fail the send", swallowed)

    SnapshotNotifier(FakeEmail(), out, keep_days=5,
                     clock=lambda: at(20) + timedelta(days=6)).send_digest(digest("e"))
    check("snapshots past keep_days are pruned",
          [r["jobs"][0]["id"] for r in load_snapshots(out)] == ["e"])

    noted = [("Intern", [("Software", [(job("w", note="mentions clearance"),
                                         Score(70, "ok", ScoreScale.LLM,
                                               work_auth_barrier=True))])])]
    SnapshotNotifier(FakeEmail(), tmp / "warn", clock=lambda: at(20)).send_digest(noted)
    warnings = load_snapshots(tmp / "warn")[0]["jobs"][0]["warnings"]
    check("note and LLM caveat each become one warning", len(warnings) == 2)
    SnapshotNotifier(FakeEmail(), tmp / "src", clock=lambda: at(20)).send_digest(
        digest("simplify:Simplify Internships:123"))
    check("the source is the uid's ats segment",
          load_snapshots(tmp / "src")[0]["jobs"][0]["source"] == "simplify")


def view_cases() -> None:
    def run(stamp: datetime, *uids: str) -> dict:
        return {"run_at": stamp.isoformat(), "footer": f"f{stamp.day}",
                "jobs": [{"id": u, "group": "Intern", "track": "Software"} for u in uids]}

    # Newest first, as load_snapshots returns them. 10/21 02:00 UTC is still 10/20 in PT.
    runs = [run(at(22), "x", "a"), run(at(21, 2), "b"), run(at(20, 20), "c")]
    now = at(22, 20).timestamp()
    view = results_gui.build_view(runs, {}, now)
    check("one block per PT day, newest first",
          [d["date"] for d in view["days"]] == ["2026-10-22", "2026-10-20"])
    check("two runs on one day share a block", view["days"][1]["runs"] == 2
          and [j["id"] for j in view["days"][1]["jobs"]] == ["b", "c"])
    check("the footer comes from the newest run", view["footer"] == "f22")

    # 10/22 13:00 PT, so 10/18 is the oldest of the five days shown.
    window = results_gui.build_view([run(at(18), "in"), run(at(17), "out")], {}, now)
    check("only the last five days are shown",
          [d["date"] for d in window["days"]] == ["2026-10-18"])

    dup = results_gui.build_view([run(at(22), "a"), run(at(20), "a", "c")], {}, now)
    check("a role in two runs shows once, under the newer",
          [[j["id"] for j in d["jobs"]] for d in dup["days"]] == [["a"], ["c"]])

    decisions = {"a": {"state": "applied", "at": now - 10, "job": {"id": "a"}},
                 "x": {"state": "skipped", "at": now, "job": {"id": "x"}},
                 "old": {"state": "skipped", "at": now - 10 * 86400, "job": {"id": "old"}}}
    view = results_gui.build_view(runs, decisions, now)
    check("decided roles leave their day block", [d["date"] for d in view["days"]] == ["2026-10-20"])
    check("handled lists newest first and drops expired skips",
          [j["id"] for j in view["handled"]] == ["x", "a"])

    followed = {"b": {"state": "following", "at": now - 3 * 86400, "job": {"id": "b"}},
                "c": {"state": "following", "at": now - 5, "job": {"id": "c"}},
                "far": {"state": "following", "at": now - 40 * 86400, "job": {"id": "far"}}}
    view = results_gui.build_view(runs, followed, now)
    check("following lists oldest first with days since, whatever the day window",
          [(j["id"], j["days"]) for j in view["following"]] == [("far", 40), ("b", 3), ("c", 0)])
    check("a followed role leaves its day block and stays out of handled",
          "b" not in [j["id"] for d in view["days"] for j in d["jobs"]]
          and view["handled"] == [])

    split = {"b": {"state": "applying", "at": now - 86400, "job": {"id": "b"}},
             "c": {"state": "following", "at": now, "job": {"id": "c"}}}
    view = results_gui.build_view(runs, split, now)
    check("applying and following each get only their own roles",
          [(j["id"], j["days"]) for j in view["applying"]] == [("b", 1)]
          and [j["id"] for j in view["following"]] == ["c"] and view["handled"] == [])


def candidate_cases(tmp: Path) -> None:
    def entry(company: str, state: str, at: float) -> dict:
        return {"state": state, "at": at, "job": {"company": company, "title": "T",
                                                   "url": "https://x/" + company,
                                                   "source": "simplify"}}

    decisions = {"1": entry("Newco", "applied", 20), "2": entry("Newco", "applied", 10),
                 "3": entry("KnownCo", "applied", 5), "4": entry("Skipped Inc", "applied", 5),
                 "5": entry("Passed", "skipped", 5)}
    found = results_gui.source_candidates(decisions, {"knownco"}, {"skipped inc"})
    check("only applied, unknown, unskipped companies are candidates", list(found) == ["Newco"])
    check("a company's roles are listed oldest first",
          [e["at"] for e in found["Newco"]] == [10, 20])

    skip = tmp / "skip.txt"
    skip.write_text("# decided\n\nSkipped Inc | staffing firm\n  Other  \n", encoding="utf-8")
    check("the skip file ignores comments and reasons",
          results_gui.read_skips(skip) == {"skipped inc", "other"})
    check("a missing skip file skips nothing", results_gui.read_skips(tmp / "none") == set())

    text = results_gui.render_candidates(found)
    check("the list names the company and its apply URL",
          "## Newco" in text and "https://x/Newco" in text and "via simplify" in text)
    check("an empty list says so", "No companies are waiting" in
          results_gui.render_candidates({}))


def api_cases(tmp: Path) -> None:
    results_gui.RESULTS_DIR = tmp / "api"
    results_gui.DECISIONS_PATH = results_gui.RESULTS_DIR / "decisions.json"
    results_gui.CANDIDATES_PATH = tmp / "candidates.md"
    results_gui.SKIP_PATH = tmp / "skip-none.txt"
    # Stamped now, because /api/jobs hides runs older than SHOW_DAYS by the real clock.
    SnapshotNotifier(FakeEmail(), results_gui.RESULTS_DIR,
                     clock=lambda: datetime.now(timezone.utc)).send_digest(
        digest("a", "b"))
    client = results_gui.app.test_client()

    def decide(uid: str, state: str):
        return client.post("/api/decide", json={"id": uid, "state": state})

    check("marking a role applied succeeds", decide("a", "applied").status_code == 200)
    check("and lists its company as a source candidate",
          "## Acme" in results_gui.CANDIDATES_PATH.read_text(encoding="utf-8"))
    ids = [j["id"] for d in client.get("/api/jobs").get_json()["days"] for j in d["jobs"]]
    check("an applied role leaves the todo list", ids == ["b"])
    check("undo puts it back", decide("a", "").status_code == 200
          and results_gui.read_decisions() == {})
    check("and takes the company off the candidate list",
          "## Acme" not in results_gui.CANDIDATES_PATH.read_text(encoding="utf-8"))
    check("an unknown role is refused as gone", decide("zzz", "skipped").status_code == 409)
    check("an unknown state is refused", decide("a", "later").status_code == 400)

    results_gui.DECISIONS_PATH.write_text("{not json", encoding="utf-8")
    data = client.get("/api/jobs").get_json()
    check("an unreadable decisions file is reported", data["unreadable"] is True)
    check("and never overwritten", decide("a", "applied").status_code == 500
          and results_gui.DECISIONS_PATH.read_text(encoding="utf-8") == "{not json")

    old = time.time() - 10 * 86400
    results_gui.DECISIONS_PATH.write_text(json.dumps({"version": 1, "items": {
        "a": {"state": "skipped", "at": old, "job": {"id": "a"}},
        "gone": {"state": "skipped", "at": old, "job": {"id": "gone"}},
        "kept": {"state": "applied", "at": old, "job": {"id": "kept"}},
        "chasing": {"state": "following", "at": old, "job": {"id": "chasing"}},
        "filling": {"state": "applying", "at": old, "job": {"id": "filling"}}}}),
        encoding="utf-8")
    decide("b", "applied")
    data = client.get("/api/jobs").get_json()
    check("an expired skip stays off the todo list after another write",
          data["days"] == [] and "a" in results_gui.read_decisions())
    check("a skip whose snapshot is gone is dropped, applied, following and applying are kept",
          set(results_gui.read_decisions()) == {"a", "b", "kept", "chasing", "filling"})
    decide("a", "following")
    following = [j["id"] for j in client.get("/api/jobs").get_json()["following"]]
    decide("a", "applied")
    after = client.get("/api/jobs").get_json()
    check("a followed role can then be marked applied",
          following == ["chasing", "a"] and [j["id"] for j in after["following"]] == ["chasing"]
          and results_gui.read_decisions()["a"]["state"] == "applied")
    decide("a", "following")
    decide("a", "applying")
    moved = client.get("/api/jobs").get_json()
    check("a followed role moves to applying",
          [j["id"] for j in moved["applying"]] == ["filling", "a"]
          and [j["id"] for j in moved["following"]] == ["chasing"])


def exit_cases() -> None:
    reason = results_gui.exit_reason
    check("waits for the first page during the grace",
          reason(10, 0, {}, False) == "")
    check("gives up when no page ever connects",
          reason(results_gui.STARTUP_GRACE + 1, 0, {}, False) == "browser never connected")
    check("keeps serving a live page", reason(100, 0, {"p": 90}, True) == "")
    check("exits once every page is gone", reason(100, 0, {}, True) == "page closed")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as raw:
        snapshot_cases(Path(raw))
        view_cases()
        candidate_cases(Path(raw))
        api_cases(Path(raw))
        exit_cases()
    print(f"{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
