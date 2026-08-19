"""The one logger for "this run did not see everything it should have".

Its own logger so attach_catchup_log() can also persist these records to a file: by the
time they matter, the run's console output is long gone. Lives apart from its writers
(fetchers pagination caps, dates normalization gaps) so neither has to import the other.
"""
from __future__ import annotations

import logging
from pathlib import Path

# Propagates, so records still show up in the normal log whether or not a file is attached.
catchup_log = logging.getLogger("jobscout.coverage")


def attach_catchup_log(path: Path) -> None:
    """Additionally append `catchup_log` records to `path` (see CATCHUP_LOG_FILENAME).
    No-op once attached, so a re-entered entry point cannot duplicate every line."""
    if catchup_log.handlers:
        return
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    catchup_log.addHandler(handler)
