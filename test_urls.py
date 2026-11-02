"""Tests for jobscout.urls.canon_url, run directly with no pytest.

    conda run --no-capture-output -n ML python test_urls.py
"""
from __future__ import annotations

import sys

from jobscout.urls import canon_url

failures = 0


def check(name: str, ok: bool) -> None:
    global failures
    print(("PASS " if ok else "FAIL ") + name)
    failures += not ok


def check_amazon_forms_fold_to_one_key() -> None:
    forms = [
        "https://www.amazon.jobs/en/jobs/10593902/software-development-engineer-database-early-career-2027-us",
        "https://amazon.jobs/en/jobs/10593902/software-development-engineer-database-early-career-2027-us",
        "https://www.amazon.jobs/jobs/10593902/apply",
        "https://www.amazon.jobs/en-gb/jobs/10593902/",
        "https://www.amazon.jobs/jobs/10593902?cmpid=abc",
    ]
    keys = {canon_url(url) for url in forms}
    check(f"amazon forms fold to one key: {keys}", keys == {"amazon.jobs:10593902"})


def check_amazon_ids_stay_apart() -> None:
    check("two amazon ids keep two keys",
          canon_url("https://www.amazon.jobs/jobs/10593902/apply")
          != canon_url("https://www.amazon.jobs/jobs/10593903/apply"))


def check_amazon_non_posting_paths_untouched() -> None:
    for url in ("https://www.amazon.jobs/en/search?base_query=sde",
                "https://www.amazon.jobs/en/teams/aws",
                "https://www.amazon.jobs/en/jobs/abc/apply"):
        check(f"non-posting amazon path not folded: {url}",
              not canon_url(url).startswith("amazon.jobs:"))


def check_lookalike_host_untouched() -> None:
    url = "https://notamazon.jobs/jobs/10593902/apply"
    check("lookalike host not folded", not canon_url(url).startswith("amazon.jobs:"))


def main() -> int:
    check_amazon_forms_fold_to_one_key()
    check_amazon_ids_stay_apart()
    check_amazon_non_posting_paths_untouched()
    check_lookalike_host_untouched()
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
