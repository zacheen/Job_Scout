# Add the companies I applied to as Job Scout sources

Work in the Job_Scout repo. Read `ignore_source_candidates.md` at the repo root. Each company in it had a role I applied to that arrived through a job board such as Simplify or SpeedyApply, and has no entry in `companies.yaml` yet. The goal is that Job Scout fetches each of these companies from its own careers feed from now on.

If the file says no companies are waiting, tell me so and stop.

## For each company, decide one of four outcomes

1. **Already a source under another spelling.** Search `companies.yaml` for the company, including comments, and compare the apply URL's ATS tenant and site with the existing entries. Decide by tenant and site, never by how similar the names look. "DellFor Technologies" is a staffing firm on SmartRecruiters and not Dell. When it is the same employer, add an alias in `jobscout/company_aliases.py` and follow the rules in that file's docstring.
2. **An existing ats fits.** Work out which ATS hosts its careers page from the apply URL and, when the URL is an aggregator redirect, from the company's own careers page. Add an entry to `companies.yaml` using the keys listed at the top of that file, and put it next to entries of the same ats.
3. **No existing ats fits.** Write a new fetcher in `jobscout/fetchers.py` following the closest existing one, register it in `FetcherFactory`, document its keys at the top of `companies.yaml`, and add the entry.
4. **Should not be added.** Examples are a staffing firm, a feed that blocks scripted requests, or a company with no feed I could reach. Append one line `Company | reason` to `ignore_source_skip.txt` at the repo root, using the company name exactly as the candidates file spells it.

A comment in `companies.yaml` saying a company was deferred or rejected counts as an earlier decision. Report it rather than overturning it.

## Verify every entry you add

Fetch the new entry on its own before keeping it. Write a throwaway script outside the repo that loads the entry with `Settings.load`, builds it with `FetcherFactory.create` and an `HttpClient`, and calls `fetch_all(EMPTY_SEEN_LEDGER)`. Run Python through `conda run -n ML python`.
- It has to return roles, and the role I applied to should be among them when it is still open.
- A wrong slug only logs a warning during a scan, so an entry that returns nothing is a failure here, not a pass.
- An entry that cannot be made to return roles goes to `ignore_source_skip.txt` with the reason instead.

After the edits, run `conda run -n ML python results_gui.py --write-candidates` and confirm every company you handled has left `ignore_source_candidates.md`.

## Limits

- Edit only `companies.yaml`, `jobscout/company_aliases.py`, `jobscout/fetchers.py` and `ignore_source_skip.txt`. Never edit `ignore_source_candidates.md` by hand, since it is regenerated.
- Do not commit, push or run `local_run.py`.
- If a company needs a decision only I can make, list it and leave it out of every file.

## Report

Give one line per company saying which outcome it got, the entry or alias you added, and how many roles the verification fetch returned. List the companies you left for me separately with the question each one needs answered.
