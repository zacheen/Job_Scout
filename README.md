# Job Scout

Polls ~100 top-tech companies' official ATS feeds every 30 minutes, keeps only
US roles, scores each new posting with an LLM on how well it fits your résumé,
and emails a sorted digest of the strong matches.
De-dupes via a CSV ledger. The cloud keeps that ledger on a separate `data`
branch (auto-created), so `main` stays code-only; local runs keep their own
gitignored copy.

## How it works

```
fetch (ATS JSON APIs) -> US filter -> dedupe (CSV) -> keyword pre-filter
  -> keyword track routing -> LLM score {experience_score, reason}
  -> experience_score > threshold -> one digest email, sorted by experience_score
```

First run **seeds only**: it records all currently-open roles as "seen" without
scoring or emailing, so you are not flooded with the existing backlog. Scoring
and email start from the next run, for genuinely new postings.

## Setup

1. Make the repo **public** (free unlimited Actions minutes). The committed
   ledger holds only job title/link/scores — no personal data.
2. Add GitHub Actions secrets (Settings → Secrets and variables → Actions):

   | Secret | Purpose |
   |---|---|
   | `OPENAI_API_KEY` | LLM scoring |
   | `RESUME_TEXT` | Your résumé as plain text (drives the experience score) |
   | `GMAIL_USER` | Sending Gmail address |
   | `GMAIL_APP_PASSWORD` | Gmail app password (not your login password) |
   | `MAIL_TO` | Where to send the digest |

3. Edit `config.yaml` (`model`, per-track `threshold` / `min_hits`, keywords) and `companies.yaml`.

## Run locally

Secrets never go in `config.yaml` or any committed file. Locally they come from a
gitignored `.env`:

```bash
conda run -n ML pip install -r requirements.txt
cp .env.example .env          # then edit .env with your values
# Resume: leave RESUME_TEXT blank in .env and drop your resume in resume.txt (gitignored)
conda run -n ML python run.py     # or: python -m jobscout
```

Run `run.py` directly (or point the VS Code debugger at it) — do **not** run
`jobscout/__main__.py` by path, or relative imports fail with no package context.

`.env` is loaded automatically. The first invocation seeds `local_data/` (one CSV
per company) and exits without scoring (so no OpenAI key is needed just to seed).

## Notes / limitations

- Workday listings expose title + location but not the full description, so
  Workday roles are pre-filtered and scored on the title only.
- Region filtering (US + Taiwan) is a location-string heuristic; bare "remote"
  can include remote roles outside those regions. Tune `include_location_terms`
  in `config.yaml`.
- A wrong company slug is skipped with a warning, not a crash.
