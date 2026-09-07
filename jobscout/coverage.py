"""The one logger for "this run did not see everything it should have".

Its own logger so the attach_* functions can route these records somewhere durable: by the
time they matter, the run's console output is long gone. Lives apart from its writers
(fetcher pagination caps, whole sources going dark, dates normalization gaps) so none of
them has to import the other. Each sink attaches separately and guards on its OWN handler,
so wiring one never silently suppresses the other.
"""
from __future__ import annotations

import logging
import os
import sys
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
