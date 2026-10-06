"""The one logger for "this run did not see everything it should have".

Its own logger so the attach_* functions can route these records somewhere durable: by the
time they matter, the run's console output is long gone. Lives apart from its writers
(fetcher pagination caps, whole sources going dark, dates normalization gaps) so none of
them has to import the other. Each sink attaches separately and guards on its OWN handler,
so wiring one never silently suppresses the other.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import date
from pathlib import Path

# Propagates, so records still show up in the normal log whether or not a file is attached.
catchup_log = logging.getLogger("jobscout.coverage")

class _AnnotationFormatter(logging.Formatter):
    """Renders a record as a GitHub Actions `::warning::` workflow command.

    Actions parses workflow commands one LINE at a time, so a raw newline inside the
    message would end the annotation and dump the rest as ordinary output — losing
    exactly the detail that makes a multi-line failure worth reporting. The documented
    escapes (%0D / %0A) keep it one physical line while still rendering as several."""

    # "%" MUST come first: the runner substitutes unconditionally, so escaping it after
    # the others would re-escape the "%" in a "%0A" this very pass just produced, and the
    # newline would come back out as the literal text "%0A".
    _ESCAPES = (("%", "%25"), ("\r", "%0D"), ("\n", "%0A"))

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for raw, escaped in self._ESCAPES:
            message = message.replace(raw, escaped)
        return f"::warning::{message}"


def attach_catchup_log(path: Path) -> None:
    """Append `catchup_log` records to `path` (see CATCHUP_LOG_FILENAME), so they outlive
    the run that produced them. No-op if a file sink is already attached, so a re-entered
    entry point cannot duplicate every line."""
    if any(isinstance(h, logging.FileHandler) for h in catchup_log.handlers):
        return
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    catchup_log.addHandler(handler)


def attach_catchup_annotations() -> None:
    """Also emit `catchup_log` records as GitHub Actions annotations. No-op off a runner.

    Not a nicety: on a runner attach_catchup_log's file is untracked scratch that dies
    with the job, and the job log needs repo-admin rights to read, so a cloud run had NO
    channel to report a coverage gap and one went unnoticed for weeks (see
    fetchers.ParallelFetcher._report_dark). The run summary page shows annotations to
    anyone who can see the repo. It shows only the first few per step though, so read them
    as "something is wrong", not as the full list — the file and the job log stay
    authoritative."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    if any(isinstance(h.formatter, _AnnotationFormatter) for h in catchup_log.handlers):
        return
    # stdout, not stderr: Actions only parses workflow commands on stdout.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_AnnotationFormatter())
    catchup_log.addHandler(handler)


class SourceStreaks:
    """Consecutive-run count per source that fetched nothing, persisted between runs.

    DARK is a source that failed or returned nothing in a way that could be a block, and one
    such run is routinely transient (planned Workday maintenance, a board mid-redeploy). EMPTY
    is a JSON board that answered with zero openings, normal for a seasonal campus board but
    over many runs a sign the company closed or moved boards. Each kind warns at its own
    threshold, and a kind change restarts the count, so a source flipping between the two
    reaches neither.

    The file holds only these sources: a source that fetched roles is not written, which resets
    its streak and prunes companies removed from companies.yaml. Only a COMPLETED fetch stage
    calls `finish`, so a run killed mid-fetch leaves the file untouched rather than resetting
    sources it never reached. On a cloud runner the file dies with the job, so streaks stay
    at 1 and the warning never fires; local runs only.
    """

    DARK = "dark"
    EMPTY = "empty"

    def __init__(self, path: Path | None, dark_threshold: int, empty_threshold: int):
        """`path` None keeps the counting in memory only."""
        self._path = path
        self._thresholds = {self.DARK: max(1, dark_threshold),
                            self.EMPTY: max(1, empty_threshold)}
        self._previous = self._load()
        self._seen: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()  # record_* run on ParallelFetcher's pool threads

    @classmethod
    def in_memory(cls) -> "SourceStreaks":
        """No file and thresholds no run reaches, so it adds no log lines of its own."""
        return cls(None, dark_threshold=sys.maxsize, empty_threshold=sys.maxsize)

    def record_dark(self, subject: str, detail: str) -> None:
        self._record(subject, self.DARK, detail)

    def record_empty(self, subject: str) -> None:
        self._record(subject, self.EMPTY, "the board answered with zero openings")

    def _record(self, subject: str, kind: str, detail: str) -> None:
        with self._lock:
            self._seen.setdefault(subject, (kind, detail))

    def finish(self) -> None:
        """Advance every streak by this run and persist. Call once per fetch stage."""
        today = date.today().isoformat()
        current = {}
        for subject, (kind, detail) in sorted(self._seen.items()):
            prev = self._previous.get(subject, {})
            if prev.get("kind", self.DARK) != kind:
                prev = {}
            current[subject] = {"kind": kind,
                                "streak": int(prev.get("streak", 0)) + 1,
                                "since": prev.get("since", today),
                                "detail": detail}
        counts = {kind: [0, 0] for kind in self._thresholds}
        for subject, entry in current.items():
            kind = entry["kind"]
            counts[kind][0] += 1
            if entry["streak"] < self._thresholds[kind]:
                continue
            counts[kind][1] += 1
            if kind == self.DARK:
                catchup_log.warning("%s: dark for %d consecutive runs since %s; last: %s",
                                    subject, entry["streak"], entry["since"], entry["detail"])
            else:
                catchup_log.warning("%s: board empty for %d consecutive runs since %s; it may "
                                    "have closed or moved to another board",
                                    subject, entry["streak"], entry["since"])
        logging.getLogger(__name__).info(
            "source streaks: %d dark this run (%d for %d+ runs), %d empty boards "
            "(%d for %d+ runs)", counts[self.DARK][0], counts[self.DARK][1],
            self._thresholds[self.DARK], counts[self.EMPTY][0], counts[self.EMPTY][1],
            self._thresholds[self.EMPTY])
        try:
            self._save(current)
        except OSError as exc:
            # Runs after the whole fetch stage, so raising here would discard every job
            # just fetched over a bookkeeping file.
            logging.getLogger(__name__).warning("could not persist %s: %s", self._path, exc)
        self._previous = current
        self._seen = {}

    def _load(self) -> dict[str, dict]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Restarting every streak at 1 costs at most `threshold` runs of silence;
            # raising would cost the whole scan over a bookkeeping file.
            logging.getLogger(__name__).warning("ignoring unreadable %s: %s", self._path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        # Keep only well-formed entries, so a hand-edited file cannot raise inside finish().
        # An entry with no "kind" predates the EMPTY kind, so finish() reads it as DARK.
        return {s: e for s, e in data.items()
                if isinstance(e, dict) and isinstance(e.get("streak"), int)
                and isinstance(e.get("since"), str)
                and e.get("kind", self.DARK) in (self.DARK, self.EMPTY)}

    def _save(self, current: dict[str, dict]) -> None:
        if self._path is None:
            return
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8", newline="\n")
        tmp.replace(self._path)
