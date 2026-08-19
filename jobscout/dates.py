"""Whatever a job board displays as a posting date -> an ISO date it can be compared on.

Every board states this differently, and two consumers depend on the answer being
comparable: the pagination early-stop (fetchers._paginate_new, against a watermark) and the
"is this the snapshot we already recorded" test (SeenLedger.seen_snapshot, against the
ledger). Normalizing in one place is what keeps those two agreeing.
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta, timezone

from .coverage import catchup_log

_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DAYS_AGO_RE = re.compile(r"(\d+)\+?\s*days?\s+ago")
_MON_D_Y_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})\b")   # Aug. 5, 2026
_D_MON_Y_RE = re.compile(r"\b(\d{1,2})-([A-Za-z]{3,9})-(\d{4})\b")           # 13-Jul-2026
_D_MON_Y_SPACED_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b")  # RFC 2822
_M_D_Y_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")   # US order: 07/29/2026
# Epoch seconds, with an optional millisecond tail (Lever sends milliseconds).
_EPOCH_RE = re.compile(r"^(\d{10})(?:\d{3})?$")
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

_warned_date_shapes: set[str] = set()
_warned_date_shapes_lock = threading.Lock()


def _to_iso(year, month, day) -> str:
    """(y, m, d) -> ISO date, or "" when the triple is not a real calendar date."""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return ""


def _parse(text: str, today: date) -> str:
    """Recognized posting-date shape -> ISO YYYY-MM-DD; "" for anything else, INCLUDING a
    shape that matches but does not name a real date (an unknown month abbreviation, say),
    so the caller warns about those too instead of failing silently."""
    if match := _ISO_DATE_PREFIX_RE.match(text):
        return _to_iso(*match.groups())
    lowered = text.lower()
    if "today" in lowered:
        return today.isoformat()
    if "yesterday" in lowered:
        return (today - timedelta(days=1)).isoformat()
    if match := _DAYS_AGO_RE.search(lowered):
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    if match := _MON_D_Y_RE.search(text):
        month, day, year = match.groups()
        return _to_iso(year, _MONTHS.get(month[:3].lower(), 0), day)
    for pattern in (_D_MON_Y_RE, _D_MON_Y_SPACED_RE):
        if match := pattern.search(text):
            day, month, year = match.groups()
            return _to_iso(year, _MONTHS.get(month[:3].lower(), 0), day)
    if match := _M_D_Y_RE.match(text):
        month, day, year = match.groups()
        return _to_iso(year, month, day)
    if match := _EPOCH_RE.match(text):
        try:
            return datetime.fromtimestamp(int(match.group(1)), timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _warn_unparsed(text: str) -> None:
    """Report a posting date no rule could read, once per SHAPE (digits collapsed to 9) so
    a board with a distinct date on every job costs one line, not one per row. The set is
    locked because ParallelFetcher runs different hosts concurrently, and a bare
    check-then-add lets two threads log the same shape; the log call itself stays outside
    the lock because whichever thread just added the shape is guaranteed to be the only one
    that will ever log it."""
    shape = re.sub(r"\d", "9", text)
    with _warned_date_shapes_lock:
        if shape in _warned_date_shapes:
            return
        _warned_date_shapes.add(shape)
    catchup_log.warning(
        "unrecognized date_posted shape %r (e.g. %r): the role's posting date is unknown, "
        "so early-stop falls back to the already-seen count and re-stamps of it cannot be "
        "detected", shape, text)


def posted_iso(raw: str, *, today: date | None = None) -> str:
    """A board's displayed posting date -> ISO YYYY-MM-DD, or "" if nothing could read it.

    _paginate_new compares this against a YYYY-MM-DD watermark with `<`, and the raw strings
    are comparable neither to that nor to each other. Un-normalized, the operator does not
    mis-order a few rows — it silently decides the entire run: "Posted Today" and
    "August 18, 2026" sort ABOVE any "2026-…", so the stop can NEVER fire and every run
    becomes a full-board pull; "07/29/2026" sorts BELOW it, so the stop fires on page one and
    the run never sees past the newest page.

    `today` is the reference for RELATIVE wording ("Posted Today", "Posted 4 Days Ago"),
    and it is the whole reason this function takes one. Default = UTC today, matching the
    watermark's basis (first_seen, which store._now() stamps in UTC). A stored row instead
    passes its OWN first_seen date, because the relative words in a saved date_posted were
    resolved against the day that row was recorded — reading them as "relative to now"
    would make every old row look re-stamped.

    "30+ Days Ago" resolves to exactly 30 days back — the newest date it can mean, which is
    the safe direction: too-new only makes a caller page deeper, never stop early.

    An empty input is a source that carries no posting date at all (SuccessFactors,
    Goldman) — normal, and not worth warning about; only an unreadable value is."""
    text = (raw or "").strip()
    if not text:
        return ""
    if iso := _parse(text, today or datetime.now(timezone.utc).date()):
        return iso
    _warn_unparsed(text)
    return ""
