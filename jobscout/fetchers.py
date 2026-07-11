"""One fetcher strategy per ATS, a shared HTTP client, and a factory."""
from __future__ import annotations

import functools
import html
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .config import Company
from .models import Job

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    """Reduce an HTML fragment to plain text (tags removed, entities decoded)."""
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub(" ", text)).strip()


def _first_match(pattern: re.Pattern, text: str) -> str:
    """First capture group of `pattern` in `text`, HTML-stripped/unescaped, or "" if absent."""
    match = pattern.search(text)
    return strip_html(match.group(1)) if match else ""


def _unix_to_date(ts: int | float | None) -> str:
    """Unix seconds -> ISO date (YYYY-MM-DD), or '' if missing/out of range."""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _balanced_span(text: str, start: int, open_ch: str, close_ch: str) -> str:
    """Extract a balanced bracket span from text[start:], skipping brackets inside string literals.
    Used to pull a JSON array/object out of surrounding JS (e.g. `AF_initDataCallback(...)`).
    Precondition: text[start] must be open_ch."""
    depth = 0
    in_str = False
    escaped = False
    quote = ""
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                in_str = False
            continue
        if c in "\"'":
            in_str = True
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


_STOP_AFTER_SEEN = 10  # page until this many already-seen openings show up (cumulative)


def _paginate_new(
    fetch_page: Callable[[int], tuple[list[Job], int | None]],
    seen: Collection[str],
    page_size: int,
    seed_max_pages: int,
    is_seed_run: bool,
    stop_after_seen: int = _STOP_AFTER_SEEN,
) -> list[Job]:
    """Collect jobs from a newest-first source, stopping once `stop_after_seen` already-seen
    UIDs have accumulated across pages — at that point we're into the backlog and the rest
    are old too. Cumulative count (not "a whole clean page") tolerates a few new/re-touched
    roles interleaved at the top.

    `is_seed_run` = this company's first appearance (no prior uids in the ledger): the cap
    bounds the pull, because there is no backlog baseline to stop against. Otherwise the cap
    is ignored — pages until the duplicate threshold or source exhausted, so no role is missed."""
    jobs: list[Job] = []
    index = 0
    seen_count = 0
    while True:
        page_jobs, total = fetch_page(index)
        jobs.extend(page_jobs)
        index += 1
        seen_count += sum(1 for job in page_jobs if job.job_uid in seen)
        fetched = index * page_size
        if (
            not page_jobs                                 # source exhausted
            or seen_count >= stop_after_seen              # reached the already-seen backlog
            or (total is not None and fetched >= total)   # covered all results
            or (is_seed_run and index >= seed_max_pages)  # company's first run: bound the pull
        ):
            break
    return jobs


def _paginate_bounded(
    fetch_page: Callable[[int], tuple[list[Job], int | None]],
    page_size: int,
    max_pages: int,
) -> tuple[list[Job], bool]:
    """Collect ALL pages of a source with no usable ordering (early-stop invalid),
    bounded by `max_pages`. Returns (jobs, exhausted); a False `exhausted` means the
    cap was hit first — the caller should warn, or results silently truncate."""
    jobs: list[Job] = []
    for index in range(max_pages):
        page_jobs, total = fetch_page(index)
        jobs.extend(page_jobs)
        if not page_jobs or (total and (index + 1) * page_size >= total):
            return jobs, True
    return jobs, False


def _throttled(method):
    """Wrap an HttpClient request method so every outbound connection is paced first.
    The one place to add per-connection behaviour later (logging, auth, metrics)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._pace()  # pre-connection
        result = method(self, *args, **kwargs)
        # post-connection hook point
        return result
    return wrapper


class HttpClient:
    """requests.Session wrapper with shared timeout/User-Agent that paces every request."""

    def __init__(self, timeout: int, user_agent: str, delay_min: float = 1.25, delay_max: float = 2.0):
        if delay_min > delay_max:
            raise ValueError(f"delay_min ({delay_min}) must be <= delay_max ({delay_max})")
        self._timeout = timeout
        self._delay_min = delay_min
        self._delay_max = delay_max
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    def _pace(self) -> None:
        """Sleep delay_min..delay_max seconds; jitter avoids a fixed (bot-like) cadence."""
        time.sleep(random.uniform(self._delay_min, self._delay_max))

    @_throttled
    def get_json(self, url: str, params: dict | None = None):
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    @_throttled
    def get_text(self, url: str, params: dict | None = None) -> str:
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.text

    @_throttled
    def post_json(self, url: str, payload: dict, headers: dict | None = None):
        # Per-call headers merge over the session's (User-Agent stays); some ATSes
        # (ByteDance) 400 unless specific gateway headers are present.
        resp = self._session.post(url, json=payload, headers=headers, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()


class AtsFetcher(ABC):
    ats_name: str = ""

    def __init__(self, company: Company, http: HttpClient):
        if not self.ats_name:
            raise TypeError(f"{type(self).__name__} must define a non-empty ats_name")
        self._company = company
        self._http = http

    @abstractmethod
    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        """Return current postings. `seen` is a pagination-efficiency hint only:
        date-ordered sources stop once a full page is already seen; others ignore it.
        Dedup is the caller's responsibility — implementations must never filter
        return values through `seen`."""
        ...

    @property
    @abstractmethod
    def host(self) -> str:
        """Network host this fetcher hits — the grouping key for parallel fetching."""
        ...

    @staticmethod
    def uid_prefix(ats: str, company_name: str) -> str:
        """uid namespace for one (ats, company) pair, shared by _uid, _company_known, and
        external callers (e.g. seed_only) that test a uid's origin. Trailing ':' stops one
        company name being a prefix of another."""
        return f"{ats}:{company_name}:"

    def _uid(self, job_id) -> str:
        return f"{self.uid_prefix(self.ats_name, self._company.name)}{job_id}"

    def _param(self, key: str) -> str:
        try:
            return self._company.params[key]
        except KeyError as exc:
            raise KeyError(
                f"{self._company.name}: missing '{key}' for ats={self.ats_name}"
            ) from exc

    def _company_known(self, seen: Collection[str]) -> bool:
        # True iff THIS company has a prior uid in `seen` — each company gets its own
        # seed cap on first appearance, regardless of what else is already in the ledger.
        prefix = self.uid_prefix(self.ats_name, self._company.name)
        return any(uid.startswith(prefix) for uid in seen)


class PaginatedFetcher(AtsFetcher):
    """Base for newest-first, paginated sources. Subclasses set `_PAGE`/`_SEED_MAX_PAGES`
    and implement `_fetch_page`; `fetch` wires them through `_paginate_new` identically, so
    every paginated source gets the same seed-cap + early-stop behaviour and a new one can't
    forget it. Correctness depends on the source returning newest-first (early-stop assumes
    once you hit already-seen roles, the rest are older) — each subclass documents why it is."""

    _PAGE: int
    _SEED_MAX_PAGES: int

    def __init__(self, company: Company, http: HttpClient):
        super().__init__(company, http)
        # Bare annotations have no runtime force (unlike the @abstractmethods): a subclass
        # missing them would only AttributeError inside fetch(), which ParallelFetcher
        # swallows into a warning ("company yields 0 jobs forever"). Fail loud at
        # construction instead, mirroring the ats_name guard in AtsFetcher.
        page, seed_max = getattr(self, "_PAGE", None), getattr(self, "_SEED_MAX_PAGES", None)
        if not isinstance(page, int) or page < 1:
            raise TypeError(f"{type(self).__name__} must define a positive _PAGE")
        if not isinstance(seed_max, int) or seed_max < 1:
            raise TypeError(f"{type(self).__name__} must define a positive _SEED_MAX_PAGES")

    @abstractmethod
    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        """Return (jobs, total) for the 0-based page `index`; total is the full result
        count when the API reports it, else None (then only empty-page/already-seen stop)."""
        ...

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        return _paginate_new(self._fetch_page, seen, self._PAGE, self._SEED_MAX_PAGES,
                             is_seed_run=not self._company_known(seen))


class GreenhouseFetcher(AtsFetcher):
    ats_name = "greenhouse"

    @property
    def host(self) -> str:
        return "boards-api.greenhouse.io"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        board = self._param("board")
        # Optional: board-specific metadata field with the real city, for boards whose
        # location.name is a policy string ("Hybrid"/"Distributed" — Cloudflare) that
        # PreFilter's location terms would reject.
        metadata_field = self._company.params.get("location_metadata", "")
        data = self._http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
            params={"content": "true"},
        )
        jobs = []
        for item in data.get("jobs", []):
            departments = item.get("departments") or []
            jobs.append(
                Job(
                    job_uid=self._uid(item["id"]),
                    company=self._company.name,
                    title=item.get("title", ""),
                    location=self._location(item, metadata_field),
                    url=item.get("absolute_url", ""),
                    description=strip_html(item.get("content", "")),
                    department=departments[0]["name"] if departments else "",
                    date_posted=item.get("updated_at", ""),
                )
            )
        return jobs

    @staticmethod
    def _location(item: dict, metadata_field: str) -> str:
        location = (item.get("location") or {}).get("name", "")
        if not metadata_field:
            return location
        for meta in item.get("metadata") or []:
            if meta.get("name") == metadata_field and meta.get("value"):
                value = meta["value"]
                extra = ", ".join(value) if isinstance(value, list) else str(value)
                return f"{location}; {extra}" if location else extra
        return location


class LeverFetcher(AtsFetcher):
    ats_name = "lever"

    @property
    def host(self) -> str:
        return "api.lever.co"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        org = self._param("org")
        # Lever returns a bare JSON array (no wrapper object, unlike other ATSes).
        data = self._http.get_json(
            f"https://api.lever.co/v0/postings/{org}", params={"mode": "json"}
        )
        jobs = []
        for item in data:
            categories = item.get("categories") or {}
            jobs.append(
                Job(
                    job_uid=self._uid(item["id"]),
                    company=self._company.name,
                    title=item.get("text", ""),
                    location=categories.get("location", ""),
                    url=item.get("hostedUrl", ""),
                    description=strip_html(
                        item.get("descriptionPlain") or item.get("description") or ""
                    ),
                    department=categories.get("team", ""),
                    date_posted=str(item.get("createdAt", "")),
                )
            )
        return jobs


class AshbyFetcher(AtsFetcher):
    ats_name = "ashby"

    @property
    def host(self) -> str:
        return "api.ashbyhq.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        org = self._param("org")
        data = self._http.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{org}",
            params={"includeCompensation": "true"},
        )
        jobs = []
        for item in data.get("jobs", []):
            jobs.append(
                Job(
                    job_uid=self._uid(item["id"]),
                    company=self._company.name,
                    title=item.get("title", ""),
                    location=item.get("location", ""),
                    url=item.get("jobUrl", ""),
                    description=strip_html(
                        item.get("descriptionPlain") or item.get("descriptionHtml") or ""
                    ),
                    department=item.get("department", ""),
                    date_posted=item.get("publishedAt", ""),
                )
            )
        return jobs


class WorkdayFetcher(PaginatedFetcher):
    """Workday CXS `jobs` endpoint; default order is newest-first, so early-stop applies.

    Optional `searchText` param scopes a shared tenant to one brand (Matterport inside
    CoStar) — but a text search returns RELEVANCE order, which breaks the early-stop
    assumption, so those companies do a bounded full pull instead (scoped boards are
    small)."""

    ats_name = "workday"
    _PAGE = 20
    _SEED_MAX_PAGES = 10
    _MAX_SEARCH_PAGES = 25  # hard bound for the searchText (relevance-ordered) mode

    @property
    def host(self) -> str:
        return self._param("host")

    def __init__(self, company: Company, http: HttpClient):
        super().__init__(company, http)
        self._search_text = company.params.get("searchText", "")

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        if self._search_text:
            jobs, exhausted = _paginate_bounded(self._fetch_page, self._PAGE,
                                                self._MAX_SEARCH_PAGES)
            if not exhausted:
                log.warning("workday %s: hit _MAX_SEARCH_PAGES (%d) before the search was "
                            "exhausted — raise the cap", self.host, self._MAX_SEARCH_PAGES)
            return jobs
        return super().fetch(seen)

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        host, tenant, site = self._param("host"), self._param("tenant"), self._param("site")
        data = self._http.post_json(
            f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
            {"appliedFacets": {}, "limit": self._PAGE,
             "offset": index * self._PAGE, "searchText": self._search_text},
        )
        jobs = [
            Job(
                job_uid=self._uid(item.get("externalPath", "")),
                company=self._company.name,
                title=item.get("title", ""),
                location=item.get("locationsText", ""),
                # externalPath alone 404s — the JD page only exists under /en-US/{site}.
                url=f"https://{host}/en-US/{site}{item.get('externalPath', '')}",
                # Workday listing API omits description; per-job fetches are too
                # costly, so these roles are matched on title only.
                description="",
                department="",
                date_posted=item.get("postedOn", ""),
            )
            for item in data.get("jobPostings", [])
        ]
        # Some tenants (e.g. Adobe) report total=0 after the first page; treat 0 as
        # unknown so pagination doesn't stop early — truly empty boards still
        # terminate via the empty-page check.
        return jobs, data.get("total") or None


class OracleFetcher(PaginatedFetcher):
    """Oracle Recruiting Candidate-Experience API (Fusion HCM). The public
    `recruitingCEJobRequisitions?finder=findReqs;siteNumber={site},...` endpoint returns the
    jobs under items[0].requisitionList, newest-first (POSTING_DATES_DESC) — so the seen-based
    early-stop applies. `host` + `site` (the CX_N siteNumber) identify the career site."""

    ats_name = "oracle"
    _PAGE = 20
    _SEED_MAX_PAGES = 10

    @property
    def host(self) -> str:
        return self._param("host")

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        host, site = self._param("host"), self._param("site")
        url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        finder = (f"findReqs;siteNumber={site},limit={self._PAGE},"
                  f"offset={index * self._PAGE},sortBy=POSTING_DATES_DESC")
        data = self._http.get_json(
            url,
            params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                    "finder": finder},
        )
        result = (data.get("items") or [{}])[0]
        jobs = [
            Job(
                job_uid=self._uid(item["Id"]),
                company=self._company.name,
                title=item.get("Title", ""),
                location=item.get("PrimaryLocation", ""),
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{item['Id']}",
                description=strip_html(item.get("ShortDescriptionStr", "")),
                department=item.get("JobFamily") or "",
                date_posted=item.get("PostedDate", ""),
            )
            for item in result.get("requisitionList", []) if item.get("Id")
        ]
        return jobs, result.get("TotalJobsCount")


class SmartRecruitersFetcher(PaginatedFetcher):
    """SmartRecruiters public postings API. `companies/{id}/postings` returns content[]
    ordered by releasedDate descending (verified across pages), so the seen-based
    early-stop applies. The listing carries no job-ad body, so roles match on title
    only — a per-posting detail fetch across 500-5000 roles is too costly. `company`
    is the SmartRecruiters companyId (jobs.smartrecruiters.com/{company})."""

    ats_name = "smartrecruiters"
    _PAGE = 100  # API max page size
    _SEED_MAX_PAGES = 10

    @property
    def host(self) -> str:
        return "api.smartrecruiters.com"

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        company_id = self._param("company")
        data = self._http.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings",
            params={"limit": self._PAGE, "offset": index * self._PAGE},
        )
        jobs = [
            Job(
                job_uid=self._uid(item["id"]),
                company=self._company.name,
                title=item.get("name", ""),
                location=self._location(item.get("location") or {}),
                url=f"https://jobs.smartrecruiters.com/{company_id}/{item['id']}",
                description="",  # listing omits the job-ad body; matched on title only
                department=self._label(item.get("department")) or self._label(item.get("function")),
                date_posted=item.get("releasedDate", ""),
            )
            for item in data.get("content", []) if item.get("id")
        ]
        return jobs, data.get("totalFound")

    @staticmethod
    def _location(loc: dict) -> str:
        # fullLocation carries the spelled-out country PreFilter matches on; fall back to parts.
        return loc.get("fullLocation") or ", ".join(
            p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
        )

    @staticmethod
    def _label(value) -> str:
        # SmartRecruiters taxonomy fields (department/function) are {id,label} objects or {}.
        return value.get("label", "") if isinstance(value, dict) else ""


class JibeFetcher(PaginatedFetcher):
    """Jibe / iCIMS Talent Cloud careers sites. `GET https://{host}/api/jobs` with
    sortBy=posted_date&descending=true returns jobs[].data newest-first (verified for AMD
    and Rivian), so the seen-based early-stop applies. Pages are 1-based, 10 rows,
    server-fixed. `host` is the careers host (careers.amd.com, careers.rivian.com)."""

    ats_name = "jibe"
    _PAGE = 10
    _SEED_MAX_PAGES = 10

    @property
    def host(self) -> str:
        return self._param("host")

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        host = self._param("host")
        data = self._http.get_json(
            f"https://{host}/api/jobs",
            params={"page": index + 1, "sortBy": "posted_date", "descending": "true"},
        )
        jobs = []
        for wrapper in data.get("jobs", []):
            item = wrapper.get("data") or {}
            if not item.get("req_id"):
                continue
            categories = item.get("categories") or []
            jobs.append(
                Job(
                    job_uid=self._uid(item["req_id"]),
                    company=self._company.name,
                    title=item.get("title", ""),
                    location=item.get("full_location", ""),
                    # canonical_url is the public JD page; apply_url is an icims LOGIN page —
                    # wrong for email links.
                    url=(item.get("meta_data") or {}).get("canonical_url")
                        or f"https://{host}/jobs/{item['req_id']}",
                    description=strip_html(item.get("description", "")),
                    department=categories[0]["name"] if categories else "",
                    date_posted=item.get("posted_date", ""),
                )
            )
        return jobs, data.get("totalCount")


class EightfoldFetcher(PaginatedFetcher):
    """Eightfold PCSX careers sites (Qualcomm, HP). `GET {host}/api/pcsx/search` with
    sort_by=timestamp returns data.positions[] newest-first by postedTs, so the seen-based
    early-stop applies. Anonymous; the LEGACY `/api/apply/v2/jobs` route 403s
    ("Not authorized for PCSX") on these tenants — don't fall back to it. `host` is the
    careers host, `domain` the tenant's Eightfold domain key."""

    ats_name = "eightfold"
    _PAGE = 10
    _SEED_MAX_PAGES = 10

    @property
    def host(self) -> str:
        return self._param("host")

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        host = self._param("host")
        data = self._http.get_json(
            f"https://{host}/api/pcsx/search",
            params={"domain": self._param("domain"), "start": index * self._PAGE,
                    "num": self._PAGE, "sort_by": "timestamp"},
        )
        payload = data.get("data") or {}
        jobs = [
            Job(
                job_uid=self._uid(item["id"]),
                company=self._company.name,
                title=item.get("name", ""),
                location="; ".join(item.get("locations") or []),
                url=f"https://{host}{item.get('positionUrl', '')}",
                description="",  # search API carries no job-ad body
                department=item.get("department") or "",
                date_posted=_unix_to_date(item.get("postedTs")),
            )
            for item in payload.get("positions", []) if item.get("id")
        ]
        return jobs, payload.get("count")


class RadancyFetcher(PaginatedFetcher):
    """Radancy / TalentBrew portals (Disney, Arm) — method G. `GET {host}{path}` returns
    JSON whose `results` field is server-rendered HTML job cards; the two `*ModuleName`
    query params are MANDATORY (without them the endpoint 200s with an empty `results`).
    SortCriteria=5 lists newest-first (job-date-posted spans confirm), so early-stop
    applies. `path` defaults to /search-jobs/results (Disney needs /en/search-jobs/results)."""

    ats_name = "radancy"
    _PAGE = 15
    _SEED_MAX_PAGES = 10

    _TOTAL_RE = re.compile(r'data-total-job-results="(\d+)"')
    # One <li> card: anchor href + data-job-id, then everything up to </li>. Card markup
    # varies per portal: Disney nests <h2> title + job-* spans INSIDE the anchor; Arm puts
    # the title as the anchor text and location/category spans AFTER it — so the title
    # falls back to the anchor text and spans are searched across the whole card.
    _CARD_RE = re.compile(
        r'<a[^>]+href="(?P<href>[^"]*/job/[^"]+)"[^>]*data-job-id="(?P<id>\d+)"[^>]*>'
        r"(?P<inner>.*?)</a>(?P<tail>.*?)</li>",
        re.DOTALL,
    )
    _H2_RE = re.compile(r"<h2>(.*?)</h2>", re.DOTALL)
    _LOCATION_RE = re.compile(r'<span class="[^"]*location[^"]*">(.*?)</span>', re.DOTALL)
    _DEPT_RE = re.compile(r'<span class="(?:job-brand|category)">(.*?)</span>', re.DOTALL)
    _DATE_RE = re.compile(r'<span class="job-date-posted">(.*?)</span>', re.DOTALL)

    @property
    def host(self) -> str:
        return self._param("host")

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        host = self._param("host")
        path = self._company.params.get("path", "/search-jobs/results")
        data = self._http.get_json(
            f"https://{host}{path}",
            params={"ActiveFacetID": 0, "CurrentPage": index + 1,
                    "RecordsPerPage": self._PAGE, "SortCriteria": 5,
                    "SearchResultsModuleName": "Search Results",
                    "SearchFiltersModuleName": "Search Filters"},
        )
        body = data.get("results") or ""
        jobs = []
        for card in self._CARD_RE.finditer(body):
            inner = card.group("inner")
            block = inner + card.group("tail")
            h2 = self._H2_RE.search(inner)
            jobs.append(
                Job(
                    job_uid=self._uid(card.group("id")),
                    company=self._company.name,
                    title=strip_html(h2.group(1) if h2 else inner),
                    location=_first_match(self._LOCATION_RE, block),
                    url=f"https://{host}{card.group('href')}",
                    description="",  # cards carry no job-ad body
                    department=_first_match(self._DEPT_RE, block),
                    date_posted=_first_match(self._DATE_RE, block),
                )
            )
        total = self._TOTAL_RE.search(body)
        return jobs, int(total.group(1)) if total else None


class SuccessFactorsFetcher(PaginatedFetcher):
    """SAP SuccessFactors Career Site Builder portals (jobs.sap.com, Hyundai's
    careers-americas). No anonymous JSON, but the /search/ page is server-rendered
    <tr class="data-row"> rows, and `sortColumn=referencedate&sortDirection=desc`
    gives newest-first — so the seen-based early-stop applies despite the rows
    carrying no visible date."""

    ats_name = "successfactors"
    _PAGE = 25  # server-fixed rows per page
    _SEED_MAX_PAGES = 10

    _ROW_RE = re.compile(r'<tr class="data-row">(.*?)</tr>', re.DOTALL)
    # Attribute order varies between the desktop and phone anchors -> lookahead on class.
    _LINK_RE = re.compile(
        r'<a(?=[^>]*class="jobTitle-link")[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.DOTALL,
    )
    _LOCATION_RE = re.compile(r'<span class="jobLocation">\s*(.*?)\s*</span>', re.DOTALL)
    _ID_RE = re.compile(r"/(\d+)/?$")

    @property
    def host(self) -> str:
        return self._param("host")

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        host = self._param("host")
        body = self._http.get_text(
            f"https://{host}/search/",
            params={"q": "", "sortColumn": "referencedate", "sortDirection": "desc",
                    "startrow": index * self._PAGE},
        )
        jobs = []
        for row in self._ROW_RE.finditer(body):
            link = self._LINK_RE.search(row.group(1))
            if not link:
                continue
            job_id = self._ID_RE.search(link.group("href"))
            if not job_id:
                continue
            jobs.append(
                Job(
                    job_uid=self._uid(job_id.group(1)),
                    company=self._company.name,
                    title=strip_html(link.group("title")),
                    location=_first_match(self._LOCATION_RE, row.group(1)),
                    url=f"https://{host}{link.group('href')}",
                    description="",  # rows carry no job-ad body
                    department="",
                    date_posted="",  # rows carry no date; ordering comes from the query
                )
            )
        return jobs, None  # no reliable total on the page; empty-page/early-stop suffice


class AvatureFetcher(AtsFetcher):
    """Avature portals that answer plain HTTP (TSMC, Lenovo, EA — Siemens' is JS-gated).
    `GET {host}/en_US/careers/SearchJobs/?jobRecordsPerPage=20&jobOffset={N}` returns
    server-rendered <article class="article--result"> cards. The server clamps
    jobRecordsPerPage per tenant (TSMC/Lenovo 10, EA 20), so paging steps by the count
    actually returned; featured cards repeat across pages -> results are deduped by id.

    Ordering is per-tenant, hence two modes: default is a bounded full-board fetch
    (TSMC leads with old evergreen posts — NOT newest-first); config `newest_first: true`
    switches to seen-based early-stop pagination for tenants verified to list newest
    first (Lenovo — its board exceeds even the full-board page cap)."""

    ats_name = "avature"
    _RPP = 20  # requested page size; the server clamps it per tenant
    _MAX_PAGES = 120  # hard bound for the full-board mode
    _SEED_MAX_PAGES = 10  # first-run cap for the newest_first mode

    _CARD_RE = re.compile(r'<article[^>]*class="[^"]*article--result[^"]*"[^>]*>(.*?)</article>',
                          re.DOTALL)
    _LINK_RE = re.compile(r'<a[^>]*href="(?P<href>[^"]*JobDetail[^"]*)"[^>]*>(?P<title>.*?)</a>',
                          re.DOTALL)
    # id lives in a jobId query param (TSMC) or as the trailing path segment (Lenovo/EA).
    _ID_QUERY_RE = re.compile(r"[?&]jobId=(\d+)")
    _ID_PATH_RE = re.compile(r"/(\d+)(?:[/?#]|$)")
    _LOCATION_RE = re.compile(r'<span class="list-item-location">(.*?)</span>', re.DOTALL)
    _DEPT_RE = re.compile(
        r'<span class="(?:list-item-careerArea|list-item-department|paragraph)">(.*?)</span>',
        re.DOTALL,
    )
    _POSTED_RE = re.compile(r"<span[^>]*>\s*(Posted:?[^<]*)</span>")
    _SUBTITLE_SPAN_RE = re.compile(
        r'<div class="article__header__text__subtitle">.*?<span[^>]*>\s*([^<]+?)\s*</span>',
        re.DOTALL,
    )
    _TOTAL_RE = re.compile(r"(\d+)\s+results")

    def __init__(self, company: Company, http: HttpClient):
        super().__init__(company, http)
        # Per-tenant page size observed from the first page in newest_first mode.
        self._clamp: int | None = None

    @property
    def host(self) -> str:
        return self._param("host")

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        # Lenovo's board is >1200 rows (past its own "999" display cap); early-stop
        # keeps steady-state runs to ~2 pages instead of paging the whole board.
        if self._company.param_bool("newest_first"):
            jobs = _paginate_new(self._fetch_page, seen, self._RPP, self._SEED_MAX_PAGES,
                                 is_seed_run=not self._company_known(seen))
        else:
            jobs = self._fetch_all_pages()
        return self._dedupe(jobs)

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        # Step by the OBSERVED page size, not _RPP — the server clamps per tenant (see __init__).
        offset = index * (self._clamp or self._RPP) if index else 0
        cards, _ = self._get_cards(offset)
        if self._clamp is None and cards:
            self._clamp = len(cards)
        return [job for card in cards if (job := self._to_job(card)) is not None], None

    def _fetch_all_pages(self) -> list[Job]:
        jobs: list[Job] = []
        offset = 0
        total = None
        for _ in range(self._MAX_PAGES):
            cards, page_total = self._get_cards(offset)
            total = total if total is not None else page_total
            if not cards:
                break
            jobs.extend(job for card in cards if (job := self._to_job(card)) is not None)
            offset += len(cards)
            if total is not None and offset >= total:
                break
        else:
            log.warning("avature %s: hit _MAX_PAGES (%d) before the board was exhausted "
                        "(total=%s) — raise the cap or set newest_first", self.host,
                        self._MAX_PAGES, total)
        return jobs

    def _get_cards(self, offset: int) -> tuple[list[str], int | None]:
        body = self._http.get_text(
            f"https://{self.host}/en_US/careers/SearchJobs/",
            params={"jobRecordsPerPage": self._RPP, "jobOffset": offset},
        )
        found = self._TOTAL_RE.search(body)
        return self._CARD_RE.findall(body), int(found.group(1)) if found else None

    @staticmethod
    def _dedupe(jobs: list[Job]) -> list[Job]:
        # Featured cards repeat across pages; first occurrence wins, order preserved.
        by_uid: dict[str, Job] = {}
        for job in jobs:
            by_uid.setdefault(job.job_uid, job)
        return list(by_uid.values())

    def _to_job(self, card: str) -> Job | None:
        link = self._LINK_RE.search(card)
        if not link:
            return None
        href = html.unescape(link.group("href"))
        job_id = self._ID_QUERY_RE.search(href) or self._ID_PATH_RE.search(href)
        if not job_id:
            return None
        # Lenovo's location span has no class — fall back to the first subtitle span
        # that isn't the Req#/Posted line.
        location = _first_match(self._LOCATION_RE, card)
        if not location:
            candidate = _first_match(self._SUBTITLE_SPAN_RE, card)
            if candidate and not candidate.startswith(("Req", "Posted")):
                location = candidate
        return Job(
            job_uid=self._uid(job_id.group(1)),
            company=self._company.name,
            title=strip_html(link.group("title")),
            location=location,
            url=href,
            description="",  # cards carry no job-ad body
            department=_first_match(self._DEPT_RE, card),
            date_posted=_first_match(self._POSTED_RE, card),
        )


class AmazonFetcher(PaginatedFetcher):
    """amazon.jobs is keyword-search (not an all-jobs board); an optional `query` narrows it,
    `normalized_country_code[]=USA` restricts to the US, and `sort=recent` lists newest first
    so the seen-based early-stop applies. `hits` is unreliable, so total is unknown and the
    page cap bounds only the first (seed) run."""

    ats_name = "amazon"
    _PAGE = 100
    _SEED_MAX_PAGES = 3

    @property
    def host(self) -> str:
        return "www.amazon.jobs"

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        query = self._company.params.get("query", "")
        data = self._http.get_json(
            "https://www.amazon.jobs/en/search.json",
            params={"base_query": query, "result_limit": self._PAGE,
                    "offset": index * self._PAGE, "sort": "recent",
                    "normalized_country_code[]": "USA"},
        )
        jobs = [
            Job(
                job_uid=self._uid(item.get("id_icims") or item["id"]),
                company=self._company.name,
                title=item.get("title", ""),
                location=item.get("location", ""),
                url="https://www.amazon.jobs" + (item.get("job_path") or ""),
                description=strip_html(item.get("description", "")),
                department=item.get("job_category", ""),
                date_posted=item.get("posted_date", ""),
            )
            for item in data.get("jobs", [])
        ]
        return jobs, None


class GoogleFetcher(PaginatedFetcher):
    """Google has no public careers API; job data is server-side-embedded as JSON inside
    an `AF_initDataCallback({key:'ds:1', data:[...]})` script block — parsed directly, no
    browser or API key needed. `sort_by=date` lists newest first, so the seen-based
    early-stop applies (the page cap bounds only the seed run); `query`/`location` optional."""

    ats_name = "google"
    # Search endpoint; also the base for per-job description URLs (see _jd_url).
    _BASE = "https://www.google.com/about/careers/applications/jobs/results"
    _PAGE = 20
    _SEED_MAX_PAGES = 5
    _CALLBACK_RE = re.compile(r"AF_initDataCallback\(")
    # Positional indices into Google's ds:1 job record (positional schema; source:
    # notes/google_probe_log.md). Must be updated if Google changes the page layout.
    _I_ID, _I_TITLE = 0, 1
    _I_RESP, _I_QUALS, _I_ABOUT = 3, 4, 10
    _I_LOC, _I_POSTED = 9, 13

    @property
    def host(self) -> str:
        return "www.google.com"

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        query = self._company.params.get("query", "")
        location = self._company.params.get("location", "United States")
        params = {"sort_by": "date", "location": location, "page": index + 1}
        if query:
            params["q"] = query
        ds1 = self._embedded_jobs(self._http.get_text(self._BASE, params=params))
        records = ds1[0] if ds1 and isinstance(ds1[0], list) else []
        total = ds1[2] if ds1 and len(ds1) > 2 and isinstance(ds1[2], int) else None
        jobs = [self._to_job(rec) for rec in records if rec and rec[self._I_ID]]
        return jobs, total

    @classmethod
    def _embedded_jobs(cls, body: str) -> list | None:
        """Locate the `ds:1` AF_initDataCallback blob in the page HTML and return its data
        array, or None if absent or unparseable."""
        for m in cls._CALLBACK_RE.finditer(body):
            try:
                obj = _balanced_span(body, body.index("{", m.end() - 1), "{", "}")
                key = re.search(r"key:\s*'([^']+)'", obj)
                data = re.search(r"data:", obj)
                if not (key and key.group(1) == "ds:1" and data):
                    continue
                # ValueError covers a missing '{'/'[' (str.index) and bad JSON
                # (JSONDecodeError is a ValueError) -> try the next callback blob.
                return json.loads(_balanced_span(obj, obj.index("[", data.end()), "[", "]"))
            except ValueError:
                continue
        return None

    def _to_job(self, rec: list) -> Job:
        locations = rec[self._I_LOC] if len(rec) > self._I_LOC else None
        loc_text = locations[0][0] if locations and locations[0] else ""
        title = rec[self._I_TITLE] if len(rec) > self._I_TITLE else ""
        # Each text block is [null, html]; join about + responsibilities + quals.
        description = " ".join(
            strip_html(rec[i][1])
            for i in (self._I_ABOUT, self._I_RESP, self._I_QUALS)
            if len(rec) > i and isinstance(rec[i], list) and len(rec[i]) > 1
        )
        return Job(
            job_uid=self._uid(rec[self._I_ID]),
            company=self._company.name,
            title=title,
            location=loc_text,
            url=self._jd_url(rec[self._I_ID], title),
            description=description,
            department="",
            date_posted=self._posted_date(rec),
        )

    @classmethod
    def _jd_url(cls, job_id, title: str) -> str:
        # Use description URL, not rec[2]'s apply/sign-in URL.
        # Google routes on the numeric id; slug is cosmetic only.
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        tail = f"{job_id}-{slug}" if slug else str(job_id)
        return f"{cls._BASE}/{tail}"

    @classmethod
    def _posted_date(cls, rec: list) -> str:
        # rec[_I_POSTED][0] is the unix-second timestamp `sort_by=date` orders on.
        try:
            return datetime.fromtimestamp(rec[cls._I_POSTED][0], timezone.utc).date().isoformat()
        except (IndexError, TypeError, ValueError, OverflowError):
            return ""


class TinderFetcher(AtsFetcher):
    """Tinder's roles live in Match Group's shared Lever org `matchgroup`, which mixes in
    every Match brand (Hinge, Match, OkCupid...) and so can't be labeled Tinder cleanly.
    This first-party proxy returns ONLY Tinder roles as `{total, data[]}`. Small single-shot
    board with no date field: return everything and let the pipeline dedupe (no early-stop)."""

    ats_name = "tinder"

    @property
    def host(self) -> str:
        return "tinderjobs.vercel.app"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        data = self._http.get_json("https://tinderjobs.vercel.app/api/jobs")
        return [
            Job(
                job_uid=self._uid(item["id"]),
                company=self._company.name,
                title=item.get("name", ""),
                location=item.get("location", ""),
                # applicationUrl is the posting page (like Lever's hostedUrl); applyUrl
                # ("/{id}/apply") would jump straight to the application form instead.
                url=item.get("applicationUrl", ""),
                description=strip_html(item.get("description", "")),
                department=item.get("department", ""),
                date_posted="",  # proxy carries no posting date
            )
            for item in data.get("data", []) if item.get("id")
        ]


class WorkableFetcher(AtsFetcher):
    """Workable-hosted boards via the anonymous v1 widget API: ONE request returns every
    posting including full descriptions (`details=true`). The newer v3 jobs API pages by
    opaque token for the same data, so v1 stays the simpler fit for these small boards.
    `account` is the apply.workable.com account slug (e.g. pony-dot-ai)."""

    ats_name = "workable"

    @property
    def host(self) -> str:
        return "apply.workable.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        account = self._param("account")
        data = self._http.get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{account}",
            params={"details": "true"},
        )
        return [
            Job(
                job_uid=self._uid(item["shortcode"]),
                company=self._company.name,
                title=item.get("title", ""),
                location=", ".join(p for p in (item.get("city"), item.get("state"),
                                               item.get("country")) if p),
                # `url` is the public posting page; `application_url` ("/apply") jumps
                # straight to the form — wrong for email links.
                url=item.get("url", ""),
                description=strip_html(item.get("description", "")),
                department=item.get("department") or "",
                date_posted=item.get("published_on", ""),
            )
            for item in data.get("jobs", []) if item.get("shortcode")
        ]


class JazzHRFetcher(AtsFetcher):
    """JazzHR-hosted boards ({account}.applytojob.com/apply). The JSON API needs a
    per-customer key, but the board page is plain server-rendered HTML: one
    `<a href=".../apply/{id}/{slug}">` per job, followed by a list-inline <ul> whose
    fa-map-marker / fa-sitemap items carry location / department. No dates and no
    descriptions -> single-shot board, roles match on title only.

    Single-shot rests on the board rendering ALL openings in one page — verified for
    Ouster (21 rows, zero pagination markers in the HTML), and plausible platform-wide
    (JazzHR is an SMB ATS). fetch() still logs a warning past _SUSPICIOUS_COUNT so a
    future large board can't silently truncate."""

    _SUSPICIOUS_COUNT = 100  # ~5x the verified board; re-verify no pagination past this

    ats_name = "jazzhr"

    # One match per job card: the apply anchor, then everything up to the closing </ul>
    # of the location/department list (`tail`). Attribute quoting varies (' vs ") and some
    # boards emit plain-http hrefs (Near Earth Autonomy), so both schemes/quotes are accepted.
    _JOB_RE = re.compile(
        r"""href=["'](?P<url>https?://[^"']+/apply/(?P<id>[A-Za-z0-9]+)/[^"']*)["'][^>]*>
            \s*(?P<title>[^<]+?)\s*</a>(?P<tail>.*?)</ul>""",
        re.DOTALL | re.VERBOSE,
    )
    _LOCATION_RE = re.compile(r"fa-map-marker[^>]*>\s*</i>\s*([^<]*)")
    _DEPT_RE = re.compile(r"fa-sitemap[^>]*>\s*</i>\s*([^<]*)")

    @property
    def host(self) -> str:
        return f"{self._param('account')}.applytojob.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        body = self._http.get_text(f"https://{self.host}/apply")
        jobs = []
        for match in self._JOB_RE.finditer(body):
            jobs.append(
                Job(
                    job_uid=self._uid(match.group("id")),
                    company=self._company.name,
                    title=html.unescape(match.group("title")).strip(),
                    location=_first_match(self._LOCATION_RE, match.group("tail")),
                    # normalize the plain-http boards' links; applytojob.com serves https fine
                    url="https://" + match.group("url").removeprefix("http://").removeprefix("https://"),
                    description="",  # board page carries no description
                    department=_first_match(self._DEPT_RE, match.group("tail")),
                    date_posted="",  # board page carries no posting date
                )
            )
        if len(jobs) >= self._SUSPICIOUS_COUNT:
            log.warning("jazzhr %s: %d rows parsed from one page — re-verify the board "
                        "doesn't paginate at this size", self.host, len(jobs))
        return jobs


class RipplingFetcher(AtsFetcher):
    """Rippling ATS public board (`api.rippling.com/platform/api/ats/v1/board/{board}/jobs`):
    one anonymous GET returns the whole board as a BARE array (like Lever). The API emits one
    row PER LOCATION with the same uuid, so rows are merged here (locations joined) — without
    the merge one posting would become several ledger entries. No dates and no descriptions
    -> single-shot board, roles match on title only."""

    ats_name = "rippling"

    @property
    def host(self) -> str:
        return "api.rippling.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        board = self._param("board")
        data = self._http.get_json(
            f"https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs"
        )
        merged = self._merge_by_uuid(data)
        return [
            Job(
                job_uid=self._uid(uuid),
                company=self._company.name,
                title=record["item"].get("name", ""),
                location="; ".join(record["locations"]),
                url=record["item"].get("url") or f"https://ats.rippling.com/{board}/jobs/{uuid}",
                description="",  # board API carries no description
                department=(record["item"].get("department") or {}).get("label", ""),
                date_posted="",  # board API carries no posting date
            )
            for uuid, record in merged.items()
        ]

    @staticmethod
    def _merge_by_uuid(data: list[dict]) -> dict[str, dict]:
        """Collapse the one-row-per-location rows: uuid -> first row + accumulated locations."""
        merged: dict[str, dict] = {}
        for item in data:
            uuid = item.get("uuid")
            if not uuid:
                continue
            location = (item.get("workLocation") or {}).get("label", "")
            record = merged.setdefault(uuid, {"item": item, "locations": []})
            if location and location not in record["locations"]:
                record["locations"].append(location)
        return merged


class GemFetcher(AtsFetcher):
    """Gem ATS public boards (jobs.gem.com/{board}). The board's own anonymous GraphQL
    endpoint serves the postings; the page's CAPTCHA gates applications only, not reads.
    No date field -> single-shot full-board fetch. JD URL is jobs.gem.com/{board}/{extId}
    (both extId and the opaque `id` resolve; extId is the stable UUID form)."""

    ats_name = "gem"

    _QUERY = (
        "query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) "
        "{ jobPostings { id extId title locations { name city isoCountry isRemote } "
        "job { department { name } locationType employmentType } } } }"
    )

    @property
    def host(self) -> str:
        return "jobs.gem.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        board = self._param("board")
        data = self._http.post_json(
            "https://jobs.gem.com/api/public/graphql",
            {"operationName": "JobBoardList", "variables": {"boardId": board},
             "query": self._QUERY},
        )
        # GraphQL reports failures as HTTP 200 + an `errors` payload — without this guard a
        # rejected query is indistinguishable from an empty board (silent 0 jobs forever).
        if data.get("errors"):
            raise ValueError(f"{self._company.name}: gem GraphQL errors: {data['errors']}")
        postings = ((data.get("data") or {}).get("oatsExternalJobPostings") or {})
        return [
            Job(
                job_uid=self._uid(item["extId"]),
                company=self._company.name,
                title=item.get("title", ""),
                location=self._join_locations(item.get("locations") or []),
                url=f"https://jobs.gem.com/{board}/{item['extId']}",
                description="",  # board API carries no description
                department=((item.get("job") or {}).get("department") or {}).get("name", ""),
                date_posted="",  # board API carries no posting date
            )
            for item in postings.get("jobPostings", []) if item.get("extId")
        ]

    @staticmethod
    def _join_locations(locations: list[dict]) -> str:
        # `name` is display text ("SF Bay Area, CA"); append "United States" to USA rows so
        # PreFilter's include terms match even when the name lacks a state code.
        parts = []
        for loc in locations:
            name = loc.get("name", "")
            country = "United States" if loc.get("isoCountry") == "USA" else ""
            parts.append(f"{name} ({country})" if country else name)
        return "; ".join(p for p in parts if p)


class ByteDanceFetcher(AtsFetcher):
    """The ByteDance "atsx" portal family: defaults target the corporate portal
    (jobs.bytedance.com); TikTok's lifeattiktok.com runs the SAME gateway, selected via
    the `host`/`website_path`/`jd_base` params. Anonymous POST, but the gateway 400s
    ("invalid request", plain text) unless BOTH the `website-path` and
    `accept-language: en-US` headers are sent; the CSRF token endpoint exists but reads
    don't need it. No date field and unknown ordering -> single-shot full-board fetch,
    bounded by _MAX_PAGES."""

    ats_name = "bytedance"
    _PAGE = 100  # verified accepted; keeps full boards to count/100 requests
    _MAX_PAGES = 50  # hard bound: full-board paging with no early-stop (TikTok ~3600)

    def __init__(self, company: Company, http: HttpClient):
        super().__init__(company, http)
        self._headers = {"website-path": company.params.get("website_path", "en"),
                         "accept-language": "en-US"}
        self._jd_base = company.params.get("jd_base", "https://joinbytedance.com/search")

    @property
    def host(self) -> str:
        return self._company.params.get("host", "jobs.bytedance.com")

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        jobs, exhausted = _paginate_bounded(self._fetch_page, self._PAGE, self._MAX_PAGES)
        if not exhausted:
            log.warning("bytedance %s: hit _MAX_PAGES (%d) without an empty batch — "
                        "board may exceed the cap; re-check pagination",
                        self.host, self._MAX_PAGES)
        return jobs

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        data = self._http.post_json(
            f"https://{self.host}/api/v1/public/supplier/search/job/posts",
            {"recruitment_id_list": [], "keyword": "",
             "limit": self._PAGE, "offset": index * self._PAGE},
            headers=self._headers,
        )
        payload = data.get("data") or {}
        batch = payload.get("job_post_list") or []
        # A missing/0 count means UNKNOWN (None) — _paginate_bounded then stops on the
        # empty-batch check only. (A 0 sentinel would read as "done" after one page.)
        return ([self._to_job(item) for item in batch if item.get("id")],
                payload.get("count") or None)

    def _to_job(self, item: dict) -> Job:
        return Job(
            job_uid=self._uid(item["id"]),
            company=self._company.name,
            title=item.get("title", ""),
            location=self._location(item.get("city_info") or {}),
            url=f"{self._jd_base}/{item['id']}",
            description=strip_html(
                f"{item.get('description', '')}\n{item.get('requirement', '')}"
            ),
            department=(item.get("job_category") or {}).get("en_name", ""),
            date_posted="",  # API carries no posting date
        )

    @staticmethod
    def _location(city_info: dict) -> str:
        # city_info nests city -> state -> country via `parent`; walk to the top so the
        # location carries the country name PreFilter's US terms can match.
        names = []
        node = city_info
        for _ in range(6):  # city -> state -> country + slack; guards a cyclic/malformed chain
            if not isinstance(node, dict) or not node:
                break
            name = node.get("en_name")
            if name and name not in names:
                names.append(name)
            node = node.get("parent")
        return ", ".join(names)


class IbmFetcher(AtsFetcher):
    """IBM's first-party Elasticsearch proxy (www-api.ibm.com/search/api/v2). Anonymous
    POST; `_source` must be an explicit field whitelist ("*"/true are rejected) and the
    index has no sortable date field -> country-filtered single-shot (the US slice is
    ~92 of ~1102 global, so server-side filtering keeps the pull small)."""

    ats_name = "ibm"
    _API = "https://www-api.ibm.com/search/api/v2"
    _PAGE = 100
    _MAX_PAGES = 10  # hard bound: no dates, no early-stop
    _FIELDS = ["title", "url", "description", "field_keyword_05", "field_keyword_08",
               "field_keyword_19"]
    _JOB_ID_RE = re.compile(r"jobId=(\d+)")

    @property
    def host(self) -> str:
        return "www-api.ibm.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        jobs, exhausted = _paginate_bounded(self._fetch_page, self._PAGE, self._MAX_PAGES)
        if not exhausted:
            log.warning("ibm %s: hit _MAX_PAGES (%d) before the results were exhausted — "
                        "re-check pagination", self.host, self._MAX_PAGES)
        return jobs

    def _fetch_page(self, index: int) -> tuple[list[Job], int | None]:
        country = self._company.params.get("country", "United States")
        data = self._http.post_json(
            self._API,
            {"appId": "careers", "scopes": ["careers2"],
             "query": {"bool": {"must": [{"match": {"field_keyword_05": country}}]}},
             "size": self._PAGE, "from": index * self._PAGE, "_source": self._FIELDS},
        )
        hits_wrapper = data.get("hits") or {}
        jobs = []
        for hit in hits_wrapper.get("hits") or []:
            source = hit.get("_source") or {}
            url = source.get("url", "")
            job_id = self._JOB_ID_RE.search(url)
            city = source.get("field_keyword_19", "")
            jobs.append(
                Job(
                    job_uid=self._uid(job_id.group(1) if job_id else hit.get("_id", "")),
                    company=self._company.name,
                    title=source.get("title", ""),
                    # field_keyword_19 is "City, US" — the bare country code misses
                    # PreFilter's include terms, so the spelled-out country is appended.
                    location=f"{city}, {country}" if city else country,
                    url=url,
                    description=source.get("description", ""),
                    department=source.get("field_keyword_08", ""),
                    date_posted="",  # index exposes no date field
                )
            )
        return jobs, (hits_wrapper.get("total") or {}).get("value")


class AtlassianFetcher(AtsFetcher):
    """Atlassian's first-party listings endpoint: one GET returns the whole board as a
    bare array. The posting page (portalJobPost.portalUrl, on icims) is used for the
    email link — applyUrl jumps straight to the application form. Note the Lever org
    `atlassian` is an EMPTY decoy (200, zero jobs); this endpoint is the real source."""

    ats_name = "atlassian"

    @property
    def host(self) -> str:
        return "www.atlassian.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        data = self._http.get_json("https://www.atlassian.com/endpoint/careers/listings")
        jobs = []
        for item in data:
            if not item.get("id"):
                continue
            portal = item.get("portalJobPost") or {}
            jobs.append(
                Job(
                    job_uid=self._uid(item["id"]),
                    company=self._company.name,
                    title=item.get("title", ""),
                    location="; ".join(item.get("locations") or []),
                    url=portal.get("portalUrl") or item.get("applyUrl", ""),
                    description=strip_html(item.get("overview", "")),
                    department=item.get("category", ""),
                    date_posted=str(portal.get("updatedDate") or ""),
                )
            )
        return jobs


class ShopifyFetcher(AtsFetcher):
    """Shopify's careers page server-renders the whole board: one anchor per job,
    href `/careers/{slug}_{uuid}`, with the title and location as the first two text
    nodes inside. Small board, no dates -> single-shot."""

    ats_name = "shopify"

    _JOB_RE = re.compile(r'href="(?P<href>/careers/[a-z0-9-]+_(?P<id>[a-f0-9-]{36}))"[^>]*>(?P<inner>.*?)</a>',
                         re.DOTALL)

    @property
    def host(self) -> str:
        return "www.shopify.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        body = self._http.get_text("https://www.shopify.com/careers")
        jobs = []
        seen_ids = set()
        for match in self._JOB_RE.finditer(body):
            if match.group("id") in seen_ids:
                continue
            seen_ids.add(match.group("id"))
            texts = [t.strip() for t in _TAG_RE.split(match.group("inner")) if t.strip()]
            jobs.append(
                Job(
                    job_uid=self._uid(match.group("id")),
                    company=self._company.name,
                    title=html.unescape(texts[0]) if texts else "",
                    location=html.unescape(texts[1]) if len(texts) > 1 else "",
                    url=f"https://www.shopify.com{match.group('href')}",
                    description="",  # board page carries no description
                    department="",
                    date_posted="",  # board page carries no posting date
                )
            )
        return jobs


class JaneStreetFetcher(AtsFetcher):
    """Jane Street's first-party positions feed (janestreet.com/jobs/main.json): one GET
    returns a bare array with full descriptions and salary ranges; internship rows are
    included. The companion internships.json feed carries NO ids or city codes — its rows
    can't get stable uids, so it is deliberately not fetched. `city` is an office CODE
    (NYC/LDN/HKG/SGP); _CITY_MAP spells out the US ones for PreFilter, unmapped codes
    pass through raw and fail the location include-terms (non-US -> intended drop)."""

    ats_name = "janestreet"
    _CITY_MAP = {"NYC": "New York, NY, United States"}

    @property
    def host(self) -> str:
        return "www.janestreet.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        data = self._http.get_json("https://www.janestreet.com/jobs/main.json")
        return [
            Job(
                job_uid=self._uid(item["id"]),
                company=self._company.name,
                title=item.get("position", ""),
                location=self._CITY_MAP.get(item.get("city", ""), item.get("city", "")),
                url=f"https://www.janestreet.com/join-jane-street/position/{item['id']}/",
                description=strip_html(item.get("overview", "")),
                department=item.get("team") or item.get("category") or "",
                date_posted="",  # feed carries no posting date
            )
            for item in data if item.get("id")
        ]


class TeamtailorFetcher(AtsFetcher):
    """Teamtailor boards via the public RSS feed ({host}/jobs.rss). The /jobs HTML page
    is client-rendered (zero anchors), but the feed is complete for small boards and —
    unusually for the single-shot fetchers — carries pubDate and a full HTML description.
    uid = the numeric id leading the /jobs/{id}-{slug} link."""

    ats_name = "teamtailor"

    _ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
    _LINK_ID_RE = re.compile(r"/jobs/(\d+)")
    # tt:location entries nest a <tt:name> each; CDATA wrapping varies by field.
    _LOCATION_RE = re.compile(r"<tt:name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</tt:name>", re.DOTALL)

    @property
    def host(self) -> str:
        return self._param("host")

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        body = self._http.get_text(f"https://{self.host}/jobs.rss")
        jobs = []
        for item in self._ITEM_RE.finditer(body):
            block = item.group(1)
            link = self._tag(block, "link")
            job_id = self._LINK_ID_RE.search(link)
            if not job_id:
                continue
            locations = self._LOCATION_RE.findall(block)
            jobs.append(
                Job(
                    job_uid=self._uid(job_id.group(1)),
                    company=self._company.name,
                    title=self._tag(block, "title"),
                    location="; ".join(loc.strip() for loc in locations),
                    url=link,
                    description=strip_html(self._tag(block, "description")),
                    department=self._tag(block, "tt:department"),
                    date_posted=self._tag(block, "pubDate"),
                )
            )
        return jobs

    @staticmethod
    def _tag(block: str, name: str) -> str:
        match = re.search(rf"<{re.escape(name)}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(name)}>",
                          block, re.DOTALL)
        return match.group(1).strip() if match else ""


class BambooHRFetcher(AtsFetcher):
    """BambooHR hosted boards: `GET {account}.bamboohr.com/careers/list` returns the whole
    board as {"meta":{"totalCount"},"result":[...]} in one anonymous request. Rows carry
    no dates or descriptions -> single-shot, title-only matching. JD URL =
    {account}.bamboohr.com/careers/{id}."""

    ats_name = "bamboohr"

    @property
    def host(self) -> str:
        return f"{self._param('account')}.bamboohr.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        data = self._http.get_json(f"https://{self.host}/careers/list")
        jobs = []
        for item in data.get("result") or []:
            if not item.get("id"):
                continue
            location = item.get("location") or {}
            parts = [location.get("city"), location.get("state")]
            jobs.append(
                Job(
                    job_uid=self._uid(item["id"]),
                    company=self._company.name,
                    title=item.get("jobOpeningName", ""),
                    location=", ".join(p for p in parts if p),
                    url=f"https://{self.host}/careers/{item['id']}",
                    description="",  # list API carries no job-ad body
                    department=item.get("departmentLabel") or "",
                    date_posted="",  # list API carries no posting date
                )
            )
        return jobs


class GithubRepoFetcher(AtsFetcher):
    """Base for aggregator sources whose postings live in a GitHub repo, read from
    raw.githubusercontent.com. All subclasses share that host, so ParallelFetcher runs
    them sequentially as one polite stream.

    Unlike single-company ATS fetchers, one repo lists MANY employers: `Job.company` is
    the real employer parsed per-row (so referral-company roles still group under Referral),
    while the uid stays namespaced by the repo entry — cross-source dedup then falls to URL."""

    _RAW_HOST = "raw.githubusercontent.com"
    _DEFAULT_BRANCH = "main"

    @property
    def host(self) -> str:
        return self._RAW_HOST

    def _raw_url(self, path: str) -> str:
        repo = self._param("repo")
        branch = self._company.params.get("branch", self._DEFAULT_BRANCH)
        return f"https://{self._RAW_HOST}/{repo}/{branch}/{path.strip()}"


class SimplifyFetcher(GithubRepoFetcher):
    """SimplifyJobs internship repos (e.g. Summer2026-Internships). Reads the repo's
    structured `.github/scripts/listings.json`, keeping only postings marked active
    and visible (SimplifyJobs' own criteria for a still-open role)."""

    ats_name = "simplify"
    _DEFAULT_BRANCH = "dev"
    _DEFAULT_PATH = ".github/scripts/listings.json"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        path = self._company.params.get("path", self._DEFAULT_PATH)
        data = self._http.get_json(self._raw_url(path))
        jobs = []
        for item in data:
            if not (item.get("active") and item.get("is_visible") and item.get("id")):
                continue
            jobs.append(
                Job(
                    job_uid=self._uid(item["id"]),
                    company=item.get("company_name", ""),
                    title=item.get("title", ""),
                    location="; ".join(item.get("locations") or []),
                    url=item.get("url", ""),
                    description="",  # listings.json carries no description; matched on title
                    department=item.get("category", ""),
                    date_posted=_unix_to_date(item.get("date_posted")),
                )
            )
        return jobs


class SpeedyApplyFetcher(GithubRepoFetcher):
    """speedyapply college-job repos (e.g. 2027-AI-College-Jobs): postings live only in
    the repo's Markdown tables, not JSON. `files` (comma-separated) picks which tables to
    read (default the USA intern + new-grad lists)."""

    ats_name = "speedyapply"
    _DEFAULT_FILES = "README.md,NEW_GRAD_USA.md"
    _COMPANY_RE = re.compile(r"<strong>(.*?)</strong>", re.S)
    # Apply link = href immediately followed by the "Apply" image; the first-cell
    # company-site link has no such image, so it never matches.
    _APPLY_RE = re.compile(r'href="([^"]+)"[^>]*>\s*<img[^>]*alt="Apply"', re.I)

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        raw = self._company.params.get("files", self._DEFAULT_FILES)
        files = [f.strip() for f in raw.split(",") if f.strip()]
        jobs: list[Job] = []
        for path in files:
            jobs.extend(self._parse_table(self._http.get_text(self._raw_url(path))))
        return jobs

    def _parse_table(self, markdown: str) -> list[Job]:
        jobs: list[Job] = []
        company = ""  # carried forward: continuation rows repeat a company without a <strong>
        for line in markdown.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 4:  # header, separator, or too few columns to be a posting
                continue
            name = self._COMPANY_RE.search(cells[0])
            if name:
                company = strip_html(name.group(1))
            url, posting_idx = self._apply_link(cells)
            if not url:  # closed/locked posting shows a lock icon, no apply link
                continue
            # Salary cell is dropped when unknown -> row has 5 or 6 columns, so locate
            # title/location relative to the apply cell, not by a fixed index.
            title = strip_html(cells[1]) if posting_idx > 1 else ""
            location = strip_html(cells[2]) if posting_idx > 2 else ""
            if not (company and title):
                continue
            jobs.append(
                Job(
                    job_uid=self._uid(url),
                    company=company,
                    title=title,
                    location=location,
                    url=url,
                    description="",  # tables carry no description; matched on title
                    department="",
                    date_posted="",  # tables show only a relative age ("6d"), not a date
                )
            )
        return jobs

    @classmethod
    def _apply_link(cls, cells: list[str]) -> tuple[str, int]:
        """Return (apply_url, cell_index) for the first cell holding an apply link, else ('', -1)."""
        for idx, cell in enumerate(cells):
            match = cls._APPLY_RE.search(cell)
            if match:
                return match.group(1), idx
        return "", -1


class FetcherFactory:
    _REGISTRY: dict[str, type[AtsFetcher]] = {
        GreenhouseFetcher.ats_name: GreenhouseFetcher,
        LeverFetcher.ats_name: LeverFetcher,
        AshbyFetcher.ats_name: AshbyFetcher,
        WorkdayFetcher.ats_name: WorkdayFetcher,
        OracleFetcher.ats_name: OracleFetcher,
        SmartRecruitersFetcher.ats_name: SmartRecruitersFetcher,
        JibeFetcher.ats_name: JibeFetcher,
        EightfoldFetcher.ats_name: EightfoldFetcher,
        RadancyFetcher.ats_name: RadancyFetcher,
        ByteDanceFetcher.ats_name: ByteDanceFetcher,
        SuccessFactorsFetcher.ats_name: SuccessFactorsFetcher,
        AvatureFetcher.ats_name: AvatureFetcher,
        IbmFetcher.ats_name: IbmFetcher,
        AtlassianFetcher.ats_name: AtlassianFetcher,
        ShopifyFetcher.ats_name: ShopifyFetcher,
        JaneStreetFetcher.ats_name: JaneStreetFetcher,
        TeamtailorFetcher.ats_name: TeamtailorFetcher,
        BambooHRFetcher.ats_name: BambooHRFetcher,
        AmazonFetcher.ats_name: AmazonFetcher,
        GoogleFetcher.ats_name: GoogleFetcher,
        TinderFetcher.ats_name: TinderFetcher,
        WorkableFetcher.ats_name: WorkableFetcher,
        JazzHRFetcher.ats_name: JazzHRFetcher,
        RipplingFetcher.ats_name: RipplingFetcher,
        GemFetcher.ats_name: GemFetcher,
        SimplifyFetcher.ats_name: SimplifyFetcher,
        SpeedyApplyFetcher.ats_name: SpeedyApplyFetcher,
    }

    @classmethod
    def create(cls, company: Company, http: HttpClient) -> AtsFetcher:
        try:
            fetcher_cls = cls._REGISTRY[company.ats]
        except KeyError as exc:
            raise ValueError(
                f"{company.name}: unknown ats '{company.ats}' "
                f"(known: {', '.join(sorted(cls._REGISTRY))})"
            ) from exc
        return fetcher_cls(company, http)


class ParallelFetcher:
    """Groups fetchers by host; host-groups run concurrently, same-host fetchers run
    sequentially in one thread — a host is never hit by two threads at once, and
    per-request pacing still applies within each sequence. Satisfies the `Fetcher` protocol."""

    def __init__(self, fetchers: list[AtsFetcher], max_workers: int = 8):
        self._fetchers = fetchers
        self._max_workers = max_workers

    def fetch_all(self, seen: Collection[str]) -> list[Job]:
        groups: dict[str, list[AtsFetcher]] = {}
        for fetcher in self._fetchers:
            try:
                groups.setdefault(fetcher.host, []).append(fetcher)
            except Exception as exc:  # a bad host config must not drop every company
                log.warning("skipping a %s company (host lookup failed): %s", fetcher.ats_name, exc)
        if not groups:
            return []
        jobs: list[Job] = []
        fetch_group = functools.partial(self._fetch_group, seen=seen)
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(groups))) as pool:
            for group_jobs in pool.map(fetch_group, groups.items()):
                jobs.extend(group_jobs)
        return jobs

    def _fetch_group(self, group: tuple[str, list[AtsFetcher]], seen: Collection[str]) -> list[Job]:
        host, fetchers = group
        started = time.perf_counter()
        jobs: list[Job] = []
        for fetcher in fetchers:
            try:
                jobs.extend(fetcher.fetch(seen))
            except Exception as exc:  # one company failing must not abort the run
                log.warning("fetch failed for a %s company: %s", fetcher.ats_name, exc)
        # Logged when this host group finishes; the timestamp + elapsed expose the slowest host.
        log.info("host %s done: %d jobs in %.1fs", host, len(jobs), time.perf_counter() - started)
        return jobs
