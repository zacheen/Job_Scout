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
    """Conservative canonical form, validated against the real ledger (2026-07-11: 21
    proven duplicate groups merged, none split; 2026-07-12 gh_jid-into-path folding below:
    ~44k URLs checked, none split, all prior merges preserved). Boards like Agility's,
    where gh_jid is the only distinguisher, stay distinct as {id}-suffixed paths."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")

    if host.removeprefix("www.") in _ATSX_HOSTS:
        job_id = path.rsplit("/", 1)[-1]
        if job_id.isdigit():
            return f"atsx:{job_id}"

    kept = []
    gh_jid = ""
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        k = key.lower()
        if k.startswith("utm_") or k in _TRACKING_PARAMS:
            continue
        if k == "gh_jid" and value:
            gh_jid = value  # folded into the path below, never kept as a query param
            continue
        kept.append((key, value))
    # Custom-careers-site boards expose the same posting two ways: a branded
    # /roles/{id} (jd_url) and the API's ?gh_jid={id} absolute_url embed. Folding
    # gh_jid into the path canonicalizes both alike so cross-source dedup holds.
    # No-op when the path already carries the id (gh_jid was purely redundant).
    if gh_jid and gh_jid not in path:
        path = f"{path}/{gh_jid}"
    # sorted + re-encoded: param order and percent-encoding differences can't
    # split identities.
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(kept)), ""))
