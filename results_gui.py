"""Local results page: the roles each local run emailed, one block per day, newest first.

local_run.py launches it after a run that emailed at least one role, unless that run was
given --no-html. Every role is a todo until marked applied or skipped, which moves it to
the collapsed Handled section, where the mark can be undone. A role marked applied on an
open page instead stays greyed out where it was until that page reloads, so a misclick
can be undone in place. A role marked applying or following moves to its own section
pinned above the days, applying first, and stays there until marked again. The process
exits on its own once no page is open.

digest_snapshot.SnapshotNotifier writes the run-*.json snapshots and this process only
reads them, while this process is the only writer of the decisions file. With atomic
replace on both sides no lock between the two processes is needed.

Run with
    conda run -n ML python results_gui.py
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

from jobscout.config import DIGEST_TZ, RESULTS_DIRNAME, Settings
from jobscout.digest_snapshot import load_snapshots, write_atomic
from jobscout.models import season_caveat_of

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / RESULTS_DIRNAME
DECISIONS_PATH = RESULTS_DIR / "decisions.json"
# Companies of applied roles that no companies.yaml entry fetches, for an LLM to turn
# into sources. Rewritten whole by this process, so it is never edited by hand.
CANDIDATES_PATH = ROOT / "ignore_source_candidates.md"
# Companies decided against, one `Company | reason` per line. Written by whoever makes
# that decision and only read here.
SKIP_PATH = ROOT / "ignore_source_skip.txt"
DECISIONS_VERSION = 1
# following marks a role still being worked on, such as a referral ask or a cover letter.
# applying marks one picked to apply to whose application is not finished yet.
STATES = ("applied", "skipped", "following", "applying")
# Each gets a section pinned above the days.
PINNED_STATES = ("applying", "following")
# Kept until unmarked, even after the snapshots listing the role are pruned.
LASTING_STATES = ("applied", *PINNED_STATES)
# Marked on an open page, these stay greyed out in their section until it reloads.
KEEP_IN_PLACE = ("applied",)
# Older todos stay in their snapshots, so widening this brings them back.
SHOW_DAYS = 5
# A skipped role leaves the Handled section after this many days. Applied ones stay.
SKIP_TTL_DAYS = 3
# Daily_Task's task list holds 8765, so both pages can be open at once.
HOST, PORT = "127.0.0.1", 8766
# Long enough for a cold browser start on a busy machine.
STARTUP_GRACE = 90
# Must clear a minute: Chrome throttles hidden-tab timers to about once a minute.
CLIENT_TIMEOUT = 150
# How long a closed page still counts as open, so a reload can arrive.
BYE_GRACE = 10
PING_EVERY_MS = 15000
WATCHDOG_TICK = 3
WEEKDAYS = "一二三四五六日"

app = Flask(__name__)


@app.before_request
def _refuse_foreign_host():
    """A DNS-rebinding page reaches 127.0.0.1 under its own hostname, so the Host
    header is what tells it apart from this page."""
    if request.host not in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
        return jsonify({"error": "unexpected host"}), 403
    return None


_lock = threading.Lock()
_started = time.time()
_clients: dict[str, float] = {}
_seen_any = False


def exit_reason(now: float, started: float, clients: dict[str, float],
                seen_any: bool) -> str:
    """Why the process should stop, or "" to keep serving. Before the first page
    connects only the startup grace can end it, since no clients yet is not every
    page closed."""
    if not seen_any:
        return "" if now - started <= STARTUP_GRACE else "browser never connected"
    live = [t for t in clients.values() if now - t <= CLIENT_TIMEOUT]
    return "" if live else "page closed"


def _touch(cid: str) -> None:
    global _seen_any
    if not cid:
        return
    with _lock:
        _seen_any = True
        _clients[cid] = time.time()


def _watchdog() -> None:
    while True:
        time.sleep(WATCHDOG_TICK)
        now = time.time()
        with _lock:
            for cid in [c for c, t in _clients.items() if now - t > CLIENT_TIMEOUT]:
                del _clients[cid]
            why = exit_reason(now, _started, _clients, _seen_any)
            if why:
                # os._exit, not sys.exit: sys.exit would only end this thread, and Flask's dev
                # server has no shutdown hook callable from outside a request.
                # Exiting under the lock keeps a decide request from being cut off
                # between its temp file and the replace.
                print("exiting: " + why)
                os._exit(0)


def read_decisions() -> dict | None:
    """id -> {state, at, job}. {} when the file does not exist yet, None when it exists
    but cannot be read, so a write never replaces a file it could not understand."""
    try:
        data = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != DECISIONS_VERSION:
        return None
    items = data.get("items")
    return items if isinstance(items, dict) else None


def live_ids(runs: list[dict]) -> set[str]:
    return {job.get("id") for run in runs for job in run.get("jobs", [])}


def prune_decisions(items: dict, live: set[str]) -> dict:
    """A skip is kept while any snapshot still lists the role, because dropping it
    sooner would put the role back on the todo list. SKIP_TTL_DAYS only hides it."""
    return {k: v for k, v in items.items() if v.get("state") in LASTING_STATES or k in live}


def _shown_as_handled(entry: dict, now: float) -> bool:
    if entry["state"] == "skipped":
        return now - entry["at"] <= SKIP_TTL_DAYS * 86400
    return entry["state"] == "applied"


def with_season(job: dict) -> dict:
    """Adds the season caveat a snapshot written before Job.season_caveat lacks. Records
    keep no description, so this recomputes from title and department only, which for a
    newer record either finds nothing or repeats a caveat it already holds."""
    caveat = season_caveat_of(job.get("title", ""), job.get("department", ""))
    warnings = job.get("warnings") or []
    if not caveat or caveat in warnings:
        return job
    return {**job, "warnings": [*warnings, caveat]}


def build_view(runs: list[dict], decisions: dict, now: float,
               keep: frozenset[str] = frozenset()) -> dict:
    """Undecided roles grouped by the day their run emailed them, newest day first,
    for the last SHOW_DAYS days counting today. A role emailed by two runs shows
    once, under the newer one.

    A role in `keep` whose mark is in KEEP_IN_PLACE stays where it was marked from, in
    its day block or in the pinned section its decision's `was` names. It carries
    `state` and the `undo` state that puts it back, and is left out of handled."""
    kept = {jid: v for jid, v in decisions.items()
            if jid in keep and v["state"] in KEEP_IN_PLACE}
    today = datetime.fromtimestamp(now, DIGEST_TZ).date()
    days: list[dict] = []
    seen: set[str] = set()
    for run in runs:
        try:
            day = datetime.fromisoformat(run["run_at"]).astimezone(DIGEST_TZ).date()
        except (KeyError, TypeError, ValueError):
            continue
        if (today - day).days >= SHOW_DAYS:
            continue
        if not days or days[-1]["date"] != day.isoformat():
            days.append({"date": day.isoformat(),
                         "label": f"{day:%m/%d} 週{WEEKDAYS[day.weekday()]}",
                         "runs": 0, "jobs": []})
        days[-1]["runs"] += 1
        for job in run.get("jobs", []):
            jid = job.get("id")
            if not jid or jid in seen:
                continue
            held = kept.get(jid)
            if jid in decisions and not (held and "was" not in held):
                continue
            seen.add(jid)
            job = with_season(job)
            days[-1]["jobs"].append({**job, "state": held["state"], "undo": ""} if held else job)
    handled = sorted(
        ({**with_season(v["job"]), "state": v["state"], "at": v["at"],
          "left": SKIP_TTL_DAYS - int((now - v["at"]) // 86400)}
         for jid, v in decisions.items() if jid not in kept and _shown_as_handled(v, now)),
        key=lambda j: j["at"], reverse=True)

    def since(v: dict, at: float) -> dict:
        return {**with_season(v["job"]), "at": at, "days": int((now - at) // 86400)}

    # Oldest first, so the role waiting longest leads. A kept role sorts by when it
    # entered the pinned state, so it keeps its place.
    pinned = {state: sorted(
        [since(v, v["at"]) for v in decisions.values() if v["state"] == state]
        + [{**since(v, v["was"]["at"]), "state": v["state"], "undo": state}
           for v in kept.values() if v.get("was", {}).get("state") == state],
        key=lambda j: j["at"]) for state in PINNED_STATES}
    return {**pinned, "days": [d for d in days if d["jobs"]], "handled": handled,
            "footer": runs[0].get("footer", "") if runs else ""}


def find_job(jid: str, runs: list[dict], decisions: dict) -> dict | None:
    for run in runs:
        for job in run.get("jobs", []):
            if job.get("id") == jid:
                return job
    entry = decisions.get(jid)
    return entry["job"] if entry else None


def read_skips(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {line.split("|", 1)[0].strip().lower() for line in lines
            if line.strip() and not line.lstrip().startswith("#")}


def source_candidates(decisions: dict, known: set[str], skips: set[str]) -> dict[str, list]:
    """Company -> its applied roles, oldest first, for companies neither fetched by a
    companies.yaml entry nor skipped. A missing entry is what marks a job-board role,
    because the only multi-employer feeds are the aggregators and their entry names
    never match a real employer (see the note above them in companies.yaml)."""
    out: dict[str, list] = {}
    for entry in sorted(decisions.values(), key=lambda e: e["at"]):
        name = entry["job"].get("company", "").strip()
        if entry["state"] == "applied" and name and name.lower() not in known | skips:
            out.setdefault(name, []).append(entry)
    return out


CANDIDATES_HEADER = """# Companies to add as Job Scout sources

Generated by results_gui.py each time a role on the results page is marked or unmarked, and by `python results_gui.py --write-candidates`. Hand edits are lost on the next write.

Every company below has a role you applied to that came from a job board, and no entry in companies.yaml. ADD_SOURCES_PROMPT.md says how to work through them. A company leaves this list once companies.yaml or ignore_source_skip.txt names it.
"""


def render_candidates(candidates: dict[str, list]) -> str:
    parts = [CANDIDATES_HEADER]
    if not candidates:
        parts.append("\nNo companies are waiting.\n")
    for name in sorted(candidates, key=str.lower):
        parts.append(f"\n## {name}\n\n")
        for entry in candidates[name]:
            job = entry["job"]
            day = datetime.fromtimestamp(entry["at"], DIGEST_TZ).date().isoformat()
            parts.append(f"- {job.get('title', '?')}, applied {day}, "
                         f"via {job.get('source', '?')}\n")
            parts.append(f"  - {job.get('url', '?')}\n")
    return "".join(parts)


def write_candidates(decisions: dict) -> None:
    """Never raises, so a broken companies.yaml cannot fail the click that triggered it."""
    try:
        known = {c.name.strip().lower() for c in Settings.load(ROOT).companies}
        text = render_candidates(source_candidates(decisions, known, read_skips(SKIP_PATH)))
        tmp = CANDIDATES_PATH.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, CANDIDATES_PATH)
    except Exception as exc:
        print("source candidates not written: " + repr(exc))


@app.get("/")
def index():
    return PAGE.replace("__PING_MS__", str(PING_EVERY_MS))


@app.get("/api/jobs")
def api_jobs():
    _touch(request.args.get("cid", ""))
    # Under the lock: on Windows an open reader makes api_decide's os.replace fail.
    with _lock:
        decisions = read_decisions()
    try:
        keep = frozenset(map(str, json.loads(request.args.get("keep") or "[]")))
    except (TypeError, ValueError):
        keep = frozenset()
    view = build_view(load_snapshots(RESULTS_DIR), decisions or {}, time.time(), keep)
    view["unreadable"] = decisions is None
    return jsonify(view)


@app.post("/api/decide")
def api_decide():
    body = request.get_json(silent=True) or {}
    jid, state = str(body.get("id", "")), str(body.get("state", ""))
    if not jid or state not in (*STATES, ""):
        return jsonify({"ok": False, "error": "bad request"}), 400
    with _lock:
        decisions = read_decisions()
        if decisions is None:
            return jsonify({"ok": False, "error": "decisions file unreadable"}), 500
        runs = load_snapshots(RESULTS_DIR)
        if state:
            job = find_job(jid, runs, decisions)
            if job is None:
                return jsonify({"ok": False, "error": "gone"}), 409
            old = decisions.get(jid, {})
            entry = {"state": state, "at": time.time(), "job": job}
            was = old.get("was")
            if was and was["state"] == state:
                # An undo back to the pinned state, so its day count resumes.
                entry["at"] = was["at"]
            elif old.get("state") in PINNED_STATES:
                entry["was"] = {"state": old["state"], "at": old["at"]}
            decisions[jid] = entry
        else:
            decisions.pop(jid, None)
        RESULTS_DIR.mkdir(exist_ok=True)
        write_atomic(DECISIONS_PATH, {"version": DECISIONS_VERSION,
                                      "items": prune_decisions(decisions, live_ids(runs))})
        write_candidates(decisions)
    return jsonify({"ok": True})


@app.post("/api/ping")
def api_ping():
    _touch(request.args.get("cid", ""))
    return jsonify({"ok": True})


@app.post("/api/bye")
def api_bye():
    """Ages only the closing page's id, so a second open tab keeps the server alive.
    Aged to BYE_GRACE before expiry rather than removed, because a reload sends this
    before the new page's first request, and removing it let a watchdog tick in that
    gap end the server."""
    _age_out(request.args.get("cid", ""), time.time())
    return jsonify({"ok": True})


def _age_out(cid: str, now: float) -> None:
    with _lock:
        if cid in _clients:
            _clients[cid] = min(_clients[cid], now - CLIENT_TIMEOUT + BYE_GRACE)


PAGE = """<!doctype html>
<html lang="zh-Hant-TW">
<meta charset="utf-8"><title>Job Scout 職缺</title>
<style>
 :root{color-scheme:dark}
 body{font:15px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif;
      background:#14161a;color:#e8eaed;margin:0;padding:28px 20px;}
 .wrap{max-width:900px;margin:0 auto}
 h1{font-size:19px;margin:0 0 4px;font-weight:600}
 .sub{color:#9aa0a6;font-size:13px;margin-bottom:22px}
 .sec{margin-bottom:26px}
 .hd{font-size:13px;font-weight:600;color:#7fb2ff;padding-bottom:9px;margin-bottom:11px;
     border-bottom:1px solid #262a31}
 .hd.old{color:#aab0b7}
 .n{font-weight:400;color:#6a6f76;font-size:12px}
 .grp{font-size:11px;letter-spacing:.06em;color:#8b9096;margin:12px 0 6px}
 .day{border:1px solid #333a45;border-radius:12px;background:#171a1f;margin-bottom:22px;
      overflow:hidden}
 .dayhd{display:flex;align-items:baseline;gap:10px;padding:12px 16px;background:#20252e;
        cursor:pointer;user-select:none}
 .dayhd .date{font-size:16px;font-weight:600;color:#aab0b7}
 .day.newest .dayhd{background:#1c2a3d}
 .day.newest .dayhd .date{color:#9cc3ff}
 .dayhd .arrow{margin-left:auto;color:#8b9096;font-size:12px}
 .daybody{padding:4px 14px 6px;border-top:1px solid #333a45}
 .day.shut .daybody{display:none}
 @media (max-width:640px){.row{flex-wrap:wrap}.acts{width:100%;justify-content:flex-end}}
 .row{display:flex;gap:13px;padding:13px 15px;border:1px solid #2a2e35;
      border-radius:10px;margin-bottom:9px;background:#1b1e23;align-items:flex-start}
 .body{flex:1;min-width:0}
 .ttl{font-weight:600;margin-bottom:3px;overflow-wrap:anywhere}
 .ttl a{color:inherit;text-decoration:none;border-bottom:1px dotted #6a7480}
 .ttl a:hover{color:#9cc3ff;border-bottom-color:#9cc3ff}
 .co{color:#aab0b7;font-weight:400}
 .meta{color:#8b9096;font-size:12px;margin-top:4px}
 .warn{color:#ffb26b;font-size:13px;margin-top:4px}
 .why{color:#c9cdd3;font-size:13px;margin-top:4px;overflow-wrap:anywhere}
 .sc{font-size:11px;font-weight:600;border-radius:5px;padding:1px 7px;margin-right:8px}
 .sc.hi{background:#1d3a26;color:#7ee09a}
 .sc.mid{background:#43351a;color:#ffc46b}
 .sc.lo{background:#262a31;color:#aab0b7}
 .acts{display:flex;gap:6px;flex:none;align-items:center}
 .btn{background:#23272e;border:1px solid #383d45;color:#cfd4da;border-radius:7px;
      padding:5px 11px;font-size:12px;cursor:pointer}
 .btn:hover{background:#2b3038;color:#fff}
 .btn:disabled{opacity:.45;cursor:default}
 .btn.ok{border-color:#2f5a3a;color:#8fe0a6}
 .st{font-size:11px;min-width:44px;text-align:right}
 .saving{color:#8b9096}.failed{color:#ff7a7a;font-weight:600}
 .row.done{opacity:.6}
 .row.kept{opacity:.45;filter:grayscale(1)}
 .tag{font-size:11px;font-weight:600;border-radius:5px;padding:1px 7px;margin-right:8px}
 .tag.applied{background:#1d3a26;color:#7ee09a}
 .tag.skipped{background:#262a31;color:#aab0b7}
 .tag.following{background:#2f2445;color:#c9a0ff}
 .follow{border:1px solid #4a3a6a;border-radius:12px;background:#1a1722;margin-bottom:22px;
         padding:0 14px 6px}
 .follow .hd{color:#c9a0ff;border-bottom-color:#3a2f52;padding-top:12px}
 .btn.fol{border-color:#4a3a6a;color:#d4b8ff}
 .tag.applying{background:#43351a;color:#ffc46b}
 .follow.apply{border-color:#6b5226;background:#1f1b14}
 .follow.apply .hd{color:#ffc46b;border-bottom-color:#4a3b20}
 .btn.app{border-color:#6b5226;color:#ffcf85}
 .tog{float:right;font-weight:400;color:#7fb2ff;cursor:pointer;font-size:12px}
 .hidden{display:none}
 .empty{color:#9aa0a6;text-align:center;padding:44px 0}
 .none{color:#6a6f76;font-size:13px;padding:2px 0 8px}
 .err{color:#ff7a7a;font-weight:600;margin-bottom:16px}
 .foot{color:#8b9096;font-size:12px;margin-top:20px;text-align:center}
 .conn{position:fixed;top:12px;right:16px;font-size:12px;padding:3px 10px;border-radius:999px;
       background:#1b1e23;border:1px solid #2a2e35;color:#8b9096}
 .conn.up{color:#7ee09a;border-color:#2f5a3a}
 .conn.down{color:#ff7a7a;border-color:#6a2f2f;font-weight:600}
</style>
<div class=conn id=conn>連線檢查中</div>
<div class=wrap>
  <h1>Job Scout 職缺</h1>
  <div class=sub id=sub>載入中</div>
  <div id=err class="err hidden"></div>
  <div class="follow apply" id=secapply>
    <div class=hd>申請中 <span class=n id=cntapply></span></div>
    <div id=listapply></div>
  </div>
  <div class=follow id=secfollow>
    <div class=hd>追蹤中 <span class=n id=cntfollow></span></div>
    <div id=listfollow></div>
  </div>
  <div id=days></div>
  <div class=sec id=sechandled>
    <div class="hd old">已處理 <span class=n id=cnthandled></span>
      <span class=tog id=toghandled>展開</span></div>
    <div id=listhandled class=hidden></div>
  </div>
  <div class=foot id=foot></div>
</div>
<script>
const el = (t,c,txt)=>{const e=document.createElement(t); if(c)e.className=c;
  if(txt!==undefined)e.textContent=txt; return e;};
// One id per page, so closing one of two tabs does not look like closing both.
const CID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));
const SKIP_DAYS = """ + str(SKIP_TTL_DAYS) + """;
const SHOW_DAYS = """ + str(SHOW_DAYS) + """;
// Collapsed days by date. load() rebuilds the list, so this outlives the DOM.
const SHUT = new Set();
// Roles marked with a KEEP_IN_PLACE state on this page. A reload empties it, which is
// what moves them to handled.
const KEPT = new Set();
const KEEP_IN_PLACE = """ + json.dumps(KEEP_IN_PLACE) + """;
const DONE_TAG = {applied:'已投遞', skipped:'略過'};
// The sections for PINNED_STATES. extra holds the buttons only that section offers.
const PINNED = [
  {state:'applying', list:'listapply', cnt:'cntapply', since:'申請中', start:'今天開始申請',
   empty:'沒有申請中的職缺。在待辦或追蹤中按「申請中」就會移到這裡。', extra:[]},
  {state:'following', list:'listfollow', cnt:'cntfollow', since:'追蹤', start:'今天開始追蹤',
   empty:'沒有追蹤中的職缺。在待辦按「追蹤」就會移到這裡。',
   extra:[['申請中','app','applying']]},
];

function badge(j){
  if(j.matches !== null && j.matches !== undefined)
    return el('span','sc lo','kw '+j.matches);
  const cls = j.score >= 75 ? 'hi' : j.score >= 60 ? 'mid' : 'lo';
  return el('span','sc '+cls, String(j.score));
}

function card(j, acts){
  const row = el('div','row'), body = el('div','body');
  const t = el('div','ttl');
  if(acts.tag) t.append(acts.tag);
  else t.append(badge(j));
  const a = el('a',null,j.title); a.href = j.url; a.target = '_blank'; a.rel = 'noopener';
  t.append(a, el('span','co',' · '+j.company));
  body.append(t);
  body.append(el('div','meta',[j.location||'?', j.department||'?',
    'posted '+(j.posted||'?')].join(' · ')));
  if(j.match_counts && j.match_counts.length || j.title_matches && j.title_matches.length){
    let kw = (j.match_counts||[]).map(p=>p[0]+' x'+p[1]).join(', ');
    if(j.title_matches && j.title_matches.length) kw += ' [title: '+j.title_matches.join(', ')+']';
    body.append(el('div','meta','keyword: '+kw));
  }
  for(const w of j.warnings||[]) body.append(el('div','warn','⚠ '+w));
  if(j.reason) body.append(el('div','why','why: '+j.reason));
  const side = el('div','acts'), st = el('span','st');
  for(const b of acts.buttons) side.append(b(st));
  side.append(st);
  row.append(body, side);
  return row;
}

function button(label, cls, id, state){
  return st => {
    const b = el('button','btn '+cls,label);
    b.onclick = async ()=>{
      b.disabled = true; st.className = 'st saving'; st.textContent = '儲存中';
      try{
        const r = await fetch('/api/decide',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({id,state})});
        if(!r.ok) throw new Error(String(r.status));
        KEEP_IN_PLACE.includes(state) ? KEPT.add(id) : KEPT.delete(id);
        await load();
      }catch(e){
        b.disabled = false; st.className = 'st failed'; st.textContent = '未儲存';
      }
    };
    return b;
  };
}

function keptCard(j){
  const row = card(j,{tag: el('span','tag '+j.state, DONE_TAG[j.state]),
                      buttons:[button('取消','',j.id,j.undo)]});
  row.classList.add('kept');
  return row;
}

function showConn(up){
  const c = document.getElementById('conn');
  c.className = 'conn '+(up ? 'up' : 'down');
  c.textContent = (up ? '● 已連線 ' : '● 已斷線 ')
    + new Date().toLocaleTimeString('zh-TW',{hour12:false});
}

async function load(){
  let d;
  const keep = encodeURIComponent(JSON.stringify([...KEPT]));
  try{ d = await (await fetch('/api/jobs?cid='+CID+'&keep='+keep)).json(); }
  catch(e){
    showConn(false);
    document.getElementById('sub').textContent = '讀不到資料，伺服器已經關閉。重新啟動 results_gui.py';
    return;
  }
  showConn(true);
  const err = document.getElementById('err');
  err.classList.toggle('hidden', !d.unreadable);
  err.textContent = d.unreadable ? 'decisions.json 讀不懂，已處理的紀錄先不顯示，按鈕也無法儲存' : '';
  for(const p of PINNED){
    const list = document.getElementById(p.list);
    list.replaceChildren();
    for(const j of d[p.state]){
      if(j.state){ list.append(keptCard(j)); continue; }
      const tag = el('span','tag '+p.state, j.days ? p.since+' '+j.days+' 天' : p.start);
      list.append(card(j,{tag, buttons:[...p.extra.map(b=>button(b[0],b[1],j.id,b[2])),
                                        button('已投遞','ok',j.id,'applied'),
                                        button('略過','',j.id,'skipped'),
                                        button('移回待辦','',j.id,'')]}));
    }
    if(!d[p.state].length) list.append(el('div','none',p.empty));
    document.getElementById(p.cnt).textContent = d[p.state].filter(j=>!j.state).length+' 筆';
  }
  const box = document.getElementById('days');
  box.replaceChildren();
  let open = 0;
  d.days.forEach((day, i)=>{
    const todo = day.jobs.filter(j=>!j.state).length;
    open += todo;
    const sec = el('div', 'day'+(i ? '' : ' newest')+(SHUT.has(day.date) ? ' shut' : ''));
    const hd = el('div','dayhd'), body = el('div','daybody');
    const arrow = el('span','arrow', SHUT.has(day.date) ? '展開 ▸' : '收起 ▾');
    hd.append(el('span','date',day.label),
              el('span','n', todo+' 筆待辦'+(day.runs>1 ? '，'+day.runs+' 次 run' : '')),
              arrow);
    hd.onclick = ()=>{
      const shut = sec.classList.toggle('shut');
      shut ? SHUT.add(day.date) : SHUT.delete(day.date);
      arrow.textContent = shut ? '展開 ▸' : '收起 ▾';
    };
    let last = '';
    for(const j of day.jobs){
      const g = j.group+' ▸ '+j.track;
      if(g !== last){ body.append(el('div','grp',g)); last = g; }
      if(j.state){ body.append(keptCard(j)); continue; }
      body.append(card(j,{buttons:[button('申請中','app',j.id,'applying'),
                                   button('追蹤','fol',j.id,'following'),
                                   button('已投遞','ok',j.id,'applied'),
                                   button('略過','',j.id,'skipped')]}));
    }
    sec.append(hd, body);
    box.append(sec);
  });
  if(!d.days.length) box.append(el('div','empty','目前沒有待辦職缺'));
  const list = document.getElementById('listhandled');
  list.replaceChildren();
  for(const j of d.handled){
    const tag = j.state === 'applied' ? el('span','tag applied','已投遞')
      : el('span','tag skipped','略過，'+(j.left<=0 ? '即將清除' : '剩 '+j.left+' 天'));
    const row = card(j,{tag, buttons:[button('取消','',j.id,'')]});
    row.classList.add('done');
    list.append(row);
  }
  document.getElementById('cnthandled').textContent = d.handled.length+' 筆';
  document.getElementById('sub').textContent =
    d.days.length+' 天，共 '+open+' 筆待辦。按申請中或追蹤會移到最上方的同名區塊，按已投遞會變灰留在原處，可以直接取消，重新整理後才移到已處理。按略過會移到已處理，在那裡可以取消。';
  document.getElementById('foot').textContent = (d.footer ? d.footer+' ' : '')
    + '只顯示最近 '+SHOW_DAYS+' 天的待辦。略過的職缺 '+SKIP_DAYS+' 天後從已處理清除。';
}

document.getElementById('toghandled').onclick = e=>{
  const hidden = document.getElementById('listhandled').classList.toggle('hidden');
  e.target.textContent = hidden ? '展開' : '收起';
};
load();
setInterval(load, 60000);
const ping = ()=>fetch('/api/ping?cid='+CID,{method:'POST'})
  .then(r=>showConn(r.ok), ()=>showConn(false));
setInterval(ping, __PING_MS__);
// A hidden tab's timers are throttled to about once a minute, so the badge can be that
// stale when the tab comes back.
addEventListener('visibilitychange', ()=>{ if(!document.hidden) ping(); });
// pagehide is the unload-side event browsers still allow a beacon from. A discarded
// tab sends none, which the ping timeout covers.
addEventListener('pagehide', ()=>{ navigator.sendBeacon('/api/bye?cid='+CID); });
</script>
"""


def port_is_taken(host: str, port: int) -> bool:
    """Occupancy only. An already-open page is the usual listener, and it picks up the
    new run on its next minute refresh."""
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-candidates", action="store_true",
                        help=f"rewrite {CANDIDATES_PATH.name} and exit without serving")
    write_only = parser.parse_args().write_candidates
    decisions_now = read_decisions()
    # Unreadable decisions would render as an empty list, which reads as all done.
    if decisions_now is None:
        print(f"{DECISIONS_PATH.name} is unreadable, {CANDIDATES_PATH.name} left as it was")
    else:
        write_candidates(decisions_now)
    if write_only:
        raise SystemExit(0 if decisions_now is not None else 1)
    url = f"http://{HOST}:{PORT}/"
    if port_is_taken(HOST, PORT):
        print(f"port {PORT} is already in use, not starting. If the results page is "
              f"the listener it is at {url}")
        raise SystemExit(0)
    print("Job Scout results at " + url)
    # After the port check, or a failed bind would still pop a tab.
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
