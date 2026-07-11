"""URL canonicalization for cross-source job identity.

canon_url produces the DEDUP KEY for "do these two links point at the same
posting?" — used by the ledger's URL index and the pipeline's email dedup.
Stored/emailed URLs keep their original strings; only comparisons go through here.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that never change WHICH posting a link opens (attribution junk
# aggregators append, e.g. "?utm_source=Simplify&ref=Simplify"). utm_* is matched
# as a prefix separately.
_TRACKING_PARAMS = frozenset({"ref", "gh_src", "lever-source", "source", "src"})

# ByteDance "atsx" portal family: the SAME posting id is served on several JD
# domains (corporate + TikTok, see ByteDanceFetcher) — collapsed to one key so
# they can't email the same opening twice. Extend this tuple for each new atsx
# portal (new jd_base) added to companies.yaml.
_ATSX_HOSTS = ("joinbytedance.com", "lifeattiktok.com")


def canon_url(url: str) -> str:
    """Conservative canonical form; validated against the real ledger (2026-07-11:
    merges 21 proven duplicate groups, splits none — boards like Agility's, where
    ?gh_jid= is the ONLY thing distinguishing 54 postings, stay distinct)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")

    if host.removeprefix("www.") in _ATSX_HOSTS:
        job_id = path.rsplit("/", 1)[-1]
        if job_id.isdigit():
            return f"atsx:{job_id}"

    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        k = key.lower()
        if k.startswith("utm_") or k in _TRACKING_PARAMS:
            continue
        # Redundant when the path already carries the id; on embedded boards
        # without a path id, gh_jid IS the identity and must stay.
        if k == "gh_jid" and value and value in path:
            continue
        kept.append((key, value))
    # sorted + re-encoded: param order and percent-encoding differences can't
    # split identities.
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(kept)), ""))
