"""Tests for digest_snapshot and results_gui, run directly with no pytest.

    conda run --no-capture-output -n ML python test_results_page.py

Every case works in a throwaway directory, so the real ignore_run_results/ is untouched.
"""
from __future__ import annotations

import functools
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
    seasonal = [Job(job_uid="s", company="Acme", title="Software Intern", location="",
                    url="https://example.com/s", description="Join us this Summer 2027."),
                Job(job_uid="t", company="Acme", title="Intern, SUMMER 2027", location="",
                    url="https://example.com/t", description="summer"),
                Job(job_uid="u", company="Acme", title="Summer2027 Intern", location="",
                    url="https://example.com/u", description="Consumer products"),
                Job(job_uid="v", company="Acme", title="Robotics Intern", location="",
                    url="https://example.com/v", description="summer",
                    department="Internships - Summer 2027")]
    SnapshotNotifier(FakeEmail(), tmp / "season", clock=lambda: at(20)).send_digest(
        [("Intern", [("Software", [(j, Score(70, "ok", ScoreScale.LLM)) for j in seasonal])])])
    check("the first field holding a season word becomes one warning",
          [r["warnings"] for r in load_snapshots(tmp / "season")[0]["jobs"]]
          == [["description says 'summer'"], ["title says 'summer'"], [],
              ["department says 'summer'"]])
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

    marked = {"a": {"state": "applied", "at": now, "job": {"id": "a"}},
              "b": {"state": "applied", "at": now, "job": {"id": "b"},
                    "was": {"state": "following", "at": now - 3 * 86400}},
              "c": {"state": "following", "at": now - 86400, "job": {"id": "c"}}}
    view = results_gui.build_view(runs, marked, now, frozenset({"a", "b", "c"}))
    check("a kept role stays in its day block, marked, with undo back to todo",
          [(j["id"], j.get("state"), j.get("undo")) for j in view["days"][0]["jobs"]]
          == [("x", None, None), ("a", "applied", "")])
    check("one marked from following stays there in its old place, with undo to following",
          [(j["id"], j.get("state"), j.get("undo"), j["days"]) for j in view["following"]]
          == [("b", "applied", "following", 3), ("c", None, None, 1)])
    check("kept roles stay out of handled", view["handled"] == [])
    check("without keep they are handled",
          [j["id"] for j in results_gui.build_view(runs, marked, now)["handled"]] == ["a", "b"])


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
    # The test client's default host is a bare localhost, which the page refuses.
    own = f"http://{results_gui.HOST}:{results_gui.PORT}/"
    get = functools.partial(client.get, base_url=own)
    post = functools.partial(client.post, base_url=own)
    check("a request under another hostname is refused",
          client.get("/api/jobs", base_url=f"http://rebound.example:{results_gui.PORT}/")
          .status_code == 403)
    check("its own address is served", get("/").status_code == 200)

    def decide(uid: str, state: str):
        return post("/api/decide", json={"id": uid, "state": state})

    check("marking a role applied succeeds", decide("a", "applied").status_code == 200)
    check("and lists its company as a source candidate",
          "## Acme" in results_gui.CANDIDATES_PATH.read_text(encoding="utf-8"))
    ids = [j["id"] for d in get("/api/jobs").get_json()["days"] for j in d["jobs"]]
    check("an applied role leaves the todo list", ids == ["b"])
    check("undo puts it back", decide("a", "").status_code == 200
          and results_gui.read_decisions() == {})
    check("and takes the company off the candidate list",
          "## Acme" not in results_gui.CANDIDATES_PATH.read_text(encoding="utf-8"))
    check("an unknown role is refused as gone", decide("zzz", "skipped").status_code == 409)
    check("an unknown state is refused", decide("a", "later").status_code == 400)

    results_gui.DECISIONS_PATH.write_text("{not json", encoding="utf-8")
    data = get("/api/jobs").get_json()
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
    data = get("/api/jobs").get_json()
    check("an expired skip stays off the todo list after another write",
          data["days"] == [] and "a" in results_gui.read_decisions())
    check("a skip whose snapshot is gone is dropped, applied, following and applying are kept",
          set(results_gui.read_decisions()) == {"a", "b", "kept", "chasing", "filling"})
    decide("a", "following")
    following = [j["id"] for j in get("/api/jobs").get_json()["following"]]
    decide("a", "applied")
    after = get("/api/jobs").get_json()
    check("a followed role can then be marked applied",
          following == ["chasing", "a"] and [j["id"] for j in after["following"]] == ["chasing"]
          and results_gui.read_decisions()["a"]["state"] == "applied")
    since = results_gui.read_decisions()["a"]["was"]["at"]
    kept = get("/api/jobs?keep=" + json.dumps(["a"])).get_json()
    check("keep holds it greyed in following",
          [(j["id"], j.get("state")) for j in kept["following"]]
          == [("chasing", None), ("a", "applied")]
          and "a" not in [j["id"] for j in kept["handled"]])
    decide("a", "following")
    back = results_gui.read_decisions()["a"]
    check("undo to following resumes its day count",
          back["state"] == "following" and back["at"] == since and "was" not in back)
    check("a malformed keep is ignored",
          get("/api/jobs?keep=%7Bnot").status_code == 200
          and get("/api/jobs?keep=5").status_code == 200)
    decide("a", "following")
    decide("a", "applying")
    moved = get("/api/jobs").get_json()
    check("a followed role moves to applying",
          [j["id"] for j in moved["applying"]] == ["filling", "a"]
          and [j["id"] for j in moved["following"]] == ["chasing"])


def season_cases() -> None:
    old = {"title": "Intern, Summer 2027", "department": "", "warnings": ["w"]}
    check("an older record gains the season caveat after its warnings",
          results_gui.with_season(old)["warnings"] == ["w", "title says 'summer'"])
    newer = {"title": "Intern, Summer 2027", "warnings": ["title says 'summer'"]}
    check("a record already holding it is left alone", results_gui.with_season(newer) is newer)
    jd = {"title": "Intern", "department": "Eng", "warnings": ["description says 'summer'"]}
    check("a description caveat gains no second one", results_gui.with_season(jd) is jd)
    check("Summer_2027 is not a whole word",
          results_gui.with_season({"title": "Summer_2027 Intern"}).get("warnings") is None)
    now = time.time()
    run = {"run_at": datetime.now(timezone.utc).isoformat(), "jobs": [{**old, "id": "d"}]}
    decided = {k: {"state": k, "at": now, "job": {**old, "id": k}} for k in ("applied", "following")}
    view = results_gui.build_view([run], decided, now)
    check("day, handled and pinned records all gain it",
          all(r["warnings"][-1] == "title says 'summer'"
              for r in (view["days"][0]["jobs"][0], view["handled"][0], view["following"][0])))


def exit_cases() -> None:
    reason = results_gui.exit_reason
    check("waits for the first page during the grace",
          reason(10, 0, {}, False) == "")
    check("gives up when no page ever connects",
          reason(results_gui.STARTUP_GRACE + 1, 0, {}, False) == "browser never connected")
    check("keeps serving a live page", reason(100, 0, {"p": 90}, True) == "")
    check("exits once every page is gone", reason(100, 0, {}, True) == "page closed")

    results_gui._clients.clear()
    results_gui._clients["p"] = 1000.0
    results_gui._age_out("p", 1000.0)
    aged = results_gui._clients["p"]
    check("a closed page outlives the next watchdog tick, so a reload can arrive",
          reason(1000 + results_gui.WATCHDOG_TICK, 0, {"p": aged}, True) == "")
    check("and ends the server once the grace is over",
          reason(1000 + results_gui.BYE_GRACE + 1, 0, {"p": aged}, True) == "page closed")
    results_gui._age_out("p", 2000.0)
    check("a second goodbye never extends the grace", results_gui._clients["p"] == aged)
    results_gui._clients.clear()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as raw:
        snapshot_cases(Path(raw))
        view_cases()
        candidate_cases(Path(raw))
        api_cases(Path(raw))
        season_cases()
        exit_cases()
    print(f"{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
