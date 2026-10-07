"""URL canonicalization for cross-source job identity.

canon_url produces the DEDUP KEY for "do these two links point at the same
posting?" — used by the ledger's URL index and the pipeline's email dedup.
Stored/emailed URLs keep their original strings; only comparisons go through here.
The one exception is a Workday job link, which models.Job shortens via workday_short_url,
so new rows store and email the short form.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that never change WHICH posting a link opens (attribution junk
# aggregators append, e.g. "?utm_source=Simplify&ref=Simplify"). utm_* is matched
# as a prefix separately.
# "embed" is the odd one out: a render flag, not attribution. Ashby serves one posting
# at both /{uuid} and /{uuid}/application?embed=true; _FORM_SUFFIXES folds the path
# suffix, but the leftover embed= param alone still split the two into separate keys.
_TRACKING_PARAMS = frozenset({"ref", "gh_src", "lever-source", "source", "src", "embed"})

# ByteDance "atsx" portal family: the SAME posting id is served on several JD
# domains (corporate + TikTok, see ByteDanceFetcher) — collapsed to one key so
# they can't email the same opening twice. Extend this tuple for each new atsx
# portal (new jd_base) added to companies.yaml.
# NOTE: fetchers._NATIVE_ID_PATTERNS separately lists these hosts (plus
# jobs.bytedance.com) for a different purpose (extracting a native job id from an
# aggregator's apply URL) — a new atsx portal here may need adding there too.
_ATSX_HOSTS = ("joinbytedance.com", "lifeattiktok.com")

# Hosted boards that serve the SAME posting at .../{uuid} and at an application-form
# child page (.../{uuid}/apply on Lever, .../{uuid}/application on Ashby) — aggregators
# link either form, splitting one posting into two keys. host -> the one suffix that
# board appends; folded only when the remaining path ends in a full posting UUID, so a
# non-posting path that merely ends in /apply can't collapse onto its parent.
_FORM_SUFFIXES = {"jobs.lever.co": "/apply", "jobs.ashbyhq.com": "/application"}
# Same UUID shape as fetchers._UUID_RE, kept independent (this module stays a leaf) —
# anchoring differs too: end-of-string here vs search-anywhere there.
_UUID_TAIL_RE = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Workday job page path: [/{locale}]/{site}/job/[{location}/]{Title_ReqId}. One posting
# varies by source in locale, site case and location segment (WorkdayFetcher builds
# /en-US/{Site}/job/{location}/..., Simplify links /{site}/job/{location}/...), so only the
# site and the last segment identify it.
# Requiring the literal /job/ is what makes the optional group take the locale rather than
# the site.
_WORKDAY_HOST_SUFFIX = ".myworkdayjobs.com"
_WORKDAY_JOB_RE = re.compile(
    r"^(?:/[a-z]{2}-[a-z]{2})?/(?P<site>[^/]+)/job/(?:[^/]+/)?(?P<slug>[^/]+)$", re.IGNORECASE)


def _workday_site_slug(host: str, path: str) -> tuple[str, str] | None:
    """(site, Title_ReqId) of a Workday job page, or None for any other link. `host`
    must already be lowercased and `path` stripped of its trailing slash."""
    if not host.endswith(_WORKDAY_HOST_SUFFIX):
        return None
    match = _WORKDAY_JOB_RE.match(path)
    return (match["site"], match["slug"]) if match else None


def workday_short_url(url: str) -> str:
    """A Workday job link as https://{host}/{site lowercased}/job/{Title_ReqId}, dropping
    the locale and location segments. Any other link is returned unchanged.

    Safe to store and email: probed on 2026-10-07 with one open posting from each of 360
    ledger tenants, the public page and the CXS detail API that WorkdayJdSource reads both
    served the same posting under the short form, while a wrong ReqId or title slug served
    none. Idempotent, since a short form matches with no location segment."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    found = _workday_site_slug(host, parts.path.rstrip("/"))
    if found is None:
        return url
    site, slug = found
    return urlunsplit((parts.scheme, host, f"/{site.lower()}/job/{slug}",
                       parts.query, parts.fragment))


def canon_url(url: str) -> str:
    """Conservative canonical form, validated against the real ledger (2026-07-11: 21
    proven duplicate groups merged, none split; 2026-07-12 gh_jid-into-path folding below:
    ~44k URLs checked, none split, all prior merges preserved; 2026-08-04 lever/ashby
    form-suffix folding: 298 ledger URLs folded, every merged group shares one posting
    UUID; 2026-09-16 embed= dropping: 300624 ledger URLs checked, 514 keys changed, 60
    groups merged, none spanning two posting UUIDs). Boards like Agility's, where gh_jid
    is the only distinguisher, stay distinct as {id}-suffixed paths.

    Workday links fold to their casefolded site and Title_ReqId, so long-form keys already
    in older ledger rows equal the short form models.Job now stores. Loading one cloud
    ledger of 365294 rows under this fold merged away 3514 duplicates, lost no source uid
    or emailed flag, and changed no non-Workday key."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")

    workday = _workday_site_slug(host, path)
    if workday is not None:
        site, slug = workday
        path = f"/{site.casefold()}/job/{slug.casefold()}"

    if host.removeprefix("www.") in _ATSX_HOSTS:
        job_id = path.rsplit("/", 1)[-1]
        if job_id.isdigit():
            return f"atsx:{job_id}"

    form_suffix = _FORM_SUFFIXES.get(host.removeprefix("www."))
    if form_suffix and path.endswith(form_suffix):
        head = path[: -len(form_suffix)]
        if _UUID_TAIL_RE.search(head):
            path = head

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
