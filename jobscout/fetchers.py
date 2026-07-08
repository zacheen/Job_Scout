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
    def post_json(self, url: str, payload: dict):
        resp = self._session.post(url, json=payload, timeout=self._timeout)
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


class GreenhouseFetcher(AtsFetcher):
    ats_name = "greenhouse"

    @property
    def host(self) -> str:
        return "boards-api.greenhouse.io"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        board = self._param("board")
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
                    location=(item.get("location") or {}).get("name", ""),
                    url=item.get("absolute_url", ""),
                    description=strip_html(item.get("content", "")),
                    department=departments[0]["name"] if departments else "",
                    date_posted=item.get("updated_at", ""),
                )
            )
        return jobs


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


class WorkdayFetcher(AtsFetcher):
    ats_name = "workday"
    _PAGE = 20
    _SEED_MAX_PAGES = 10

    @property
    def host(self) -> str:
        return self._param("host")

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        host, tenant, site = self._param("host"), self._param("tenant"), self._param("site")
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

        def page(index: int) -> tuple[list[Job], int | None]:
            data = self._http.post_json(
                url,
                {"appliedFacets": {}, "limit": self._PAGE,
                 "offset": index * self._PAGE, "searchText": ""},
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

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES,
                             is_seed_run=not self._company_known(seen))  # newest-first -> early-stop applies


class OracleFetcher(AtsFetcher):
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

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        host, site = self._param("host"), self._param("site")
        url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

        def page(index: int) -> tuple[list[Job], int | None]:
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

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES,
                             is_seed_run=not self._company_known(seen))


class SmartRecruitersFetcher(AtsFetcher):
    """SmartRecruiters public postings API (Intuitive, Bosch, and many large employers).
    `companies/{id}/postings` returns content[] ordered by releasedDate descending (verified
    across pages), so the seen-based early-stop applies. The listing carries no job-ad body,
    so roles match on title only — a per-posting detail fetch across 500-5000 roles is too
    costly. `company` is the SmartRecruiters companyId (jobs.smartrecruiters.com/{company})."""

    ats_name = "smartrecruiters"
    _PAGE = 100  # API max page size
    _SEED_MAX_PAGES = 10

    @property
    def host(self) -> str:
        return "api.smartrecruiters.com"

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        company_id = self._param("company")
        url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"

        def page(index: int) -> tuple[list[Job], int | None]:
            data = self._http.get_json(
                url, params={"limit": self._PAGE, "offset": index * self._PAGE}
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

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES,
                             is_seed_run=not self._company_known(seen))

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


class AmazonFetcher(AtsFetcher):
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

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        query = self._company.params.get("query", "")

        def page(index: int) -> tuple[list[Job], int | None]:
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

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES,
                             is_seed_run=not self._company_known(seen))


class GoogleFetcher(AtsFetcher):
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

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        query = self._company.params.get("query", "")
        location = self._company.params.get("location", "United States")

        def page(index: int) -> tuple[list[Job], int | None]:
            params = {"sort_by": "date", "location": location, "page": index + 1}
            if query:
                params["q"] = query
            ds1 = self._embedded_jobs(self._http.get_text(self._BASE, params=params))
            records = ds1[0] if ds1 and isinstance(ds1[0], list) else []
            total = ds1[2] if ds1 and len(ds1) > 2 and isinstance(ds1[2], int) else None
            jobs = [self._to_job(rec) for rec in records if rec and rec[self._I_ID]]
            return jobs, total

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES,
                             is_seed_run=not self._company_known(seen))

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
        AmazonFetcher.ats_name: AmazonFetcher,
        GoogleFetcher.ats_name: GoogleFetcher,
        TinderFetcher.ats_name: TinderFetcher,
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
