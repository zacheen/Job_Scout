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


def _paginate_new(
    fetch_page: Callable[[int], tuple[list[Job], int | None]],
    seen: Collection[str],
    page_size: int,
    seed_max_pages: int,
) -> list[Job]:
    """Page through a NEWEST-FIRST source, collecting jobs until a whole page has no
    unseen job — the rest are older / already seen. Stopping on a fully-seen page (not the
    first seen job) tolerates an old role being re-touched to the top.

    An empty `seen` is treated as a seed run and the page cap bounds it — by design, since
    early-stop has no baseline to stop at yet (the pipeline's known_uids is empty only on a
    genuine (re)seed). With a non-empty `seen` the cap never applies: later runs page on
    until the all-seen wall (so no new role is missed) or the source runs out.
    `fetch_page(index)` returns (that page's jobs, total count or None if unknown)."""
    jobs: list[Job] = []
    index = 0
    while True:
        page_jobs, total = fetch_page(index)
        jobs.extend(page_jobs)
        index += 1
        has_new = any(job.job_uid not in seen for job in page_jobs)
        fetched = index * page_size
        # Empty page (source exhausted) is the first guard; the rest assume a non-empty page.
        if (
            not page_jobs
            or not has_new
            or (total is not None and fetched >= total)
            or (not seen and index >= seed_max_pages)
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

    def _uid(self, job_id) -> str:
        return f"{self.ats_name}:{self._company.name}:{job_id}"

    def _param(self, key: str) -> str:
        try:
            return self._company.params[key]
        except KeyError as exc:
            raise KeyError(
                f"{self._company.name}: missing '{key}' for ats={self.ats_name}"
            ) from exc


class GreenhouseFetcher(AtsFetcher):
    ats_name = "greenhouse"

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
                    url=f"https://{host}{item.get('externalPath', '')}",
                    # Workday listing API omits description; per-job fetches are too
                    # costly, so these roles are matched on title only.
                    description="",
                    department="",
                    date_posted=item.get("postedOn", ""),
                )
                for item in data.get("jobPostings", [])
            ]
            return jobs, data.get("total")

        # Workday defaults to newest-first, so the seen-based early-stop applies.
        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES)


class AmazonFetcher(AtsFetcher):
    """amazon.jobs is keyword-search (not an all-jobs board); an optional `query` narrows
    it and `sort=recent` lists newest first, so the seen-based early-stop applies. `hits`
    is unreliable, so total is unknown and the page cap bounds only the first (seed) run."""

    ats_name = "amazon"
    _PAGE = 100
    _SEED_MAX_PAGES = 3

    def fetch(self, seen: Collection[str] = frozenset()) -> list[Job]:
        query = self._company.params.get("query", "")

        def page(index: int) -> tuple[list[Job], int | None]:
            data = self._http.get_json(
                "https://www.amazon.jobs/en/search.json",
                params={"base_query": query, "result_limit": self._PAGE,
                        "offset": index * self._PAGE, "sort": "recent"},
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
            return jobs, None  # `hits` is unreliable -> total unknown

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES)


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

        return _paginate_new(page, seen, self._PAGE, self._SEED_MAX_PAGES)

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


class FetcherFactory:
    _REGISTRY: dict[str, type[AtsFetcher]] = {
        GreenhouseFetcher.ats_name: GreenhouseFetcher,
        LeverFetcher.ats_name: LeverFetcher,
        AshbyFetcher.ats_name: AshbyFetcher,
        WorkdayFetcher.ats_name: WorkdayFetcher,
        AmazonFetcher.ats_name: AmazonFetcher,
        GoogleFetcher.ats_name: GoogleFetcher,
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
