# jobhunt

A personal job-search agent. It reads public ATS APIs every morning, throws away
the ~99% that don't fit you, scores what's left against your resume, drafts an
application kit for the best few, and emails you a digest.

**It never submits an application.** It finds, filters, ranks and drafts. You
read the digest, edit the cover note, and press submit yourself.

```
2000 postings  →  40 candidates  →  5 in your inbox
   fetch          regex/location      LLM screen
                  /freshness gate     + draft
                  (free, no LLM)
```

> **New to Python?** Read **[SETUP.md](SETUP.md)** instead — it's a 13-step guide
> that assumes you have nothing installed. This README assumes you're comfortable
> with a terminal.

---

## Run it in 30 seconds, no API key

```bash
git clone <your-repo> && cd jobhunt
python -m venv .venv && .venv/Scripts/activate      # Windows
# python -m venv .venv && source .venv/bin/activate # macOS/Linux
pip install -r requirements.txt

python -m jobhunt run --mock --scorer keyword
```

`--mock` runs bundled fixtures through the **real parsers** — no network.
`--scorer keyword` swaps the LLM for a dumb token-overlap stub, so the whole
pipeline runs with no secrets configured. You should see:

```
[2/5] filtering
  prefilter: 12 -> 5 (dropped title=5 location=1 stale=1)
[3/5] screening 5 jobs (keyword stub — DEV ONLY)
  3 scored >= 7.0
[5/5] digest
  wrote out/digest.html

funnel: 12 scanned -> 5 passed filters -> 5 new -> 3 in digest
```

Open `out/digest.html` in a browser. That's the email you'd have received.

> The keyword scorer is **dev-only**. It cannot tell a Staff role from a
> new-grad one and has no idea what the words mean. It exists to prove the
> plumbing, never to build a digest you'd act on.

---

## Set it up for real

### 1. Point it at companies you'd actually join

Edit `companies.yaml`. The slug is the last path segment of a company's public
careers board:

| Board URL | `ats` | `slug` |
|---|---|---|
| `boards.greenhouse.io/stripe` | `greenhouse` | `stripe` |
| `jobs.lever.co/netlify` | `lever` | `netlify` |
| `jobs.ashbyhq.com/ramp` | `ashby` | `ramp` |

**Workday** sites take the full careers-page URL instead of a slug — tenant,
cluster and site are parsed from it:

```yaml
- {ats: workday, url: "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", name: NVIDIA}
```

Workday search results carry no description, so each posting that survives the
title/location/age filters costs one extra request. Search terms and page depth
are set under `workday:` in `config.yaml`.

The shipped list is **examples** — verify each before trusting the output.
Companies migrate between ATS vendors and slugs go dead. A dead slug prints an
HTTP status and returns nothing; it never kills the run. Watch the per-board
counts on stdout: a board reporting 0 every day is a slug that needs fixing.

Start with 10–15 companies. A list of 200 is mostly noise.

**No LinkedIn or Naukri.** Neither has a public API and scraping them violates
their terms of service. The three ATS endpoints above are documented, unauthenticated,
and intended to be read.

### 2. Tune the filters

`config.yaml` holds the deterministic gate that runs **before** any LLM call.
This is the whole cost story — get it right and you spend cents a day.

```yaml
filters:
  include_titles: ['\bsde\b', 'software development engineer', ...]
  exclude_titles: ['\b(staff|principal)\b', '\b(manager)\b', ...]
  locations: [bangalore, bengaluru, india]
  allow_remote: true
  max_age_days: 30
score_threshold: 7.0
max_per_digest: 5
```

> **`sde` does not match "Software Development Engineer".** They share no
> substring. Use `\bsde\b` for the acronym *and* list the spelled-out variants
> separately, or you'll silently miss half of Amazon-style postings. There's a
> test pinning this.

### 3. Build your profile

```bash
cp .env.example .env      # add ANTHROPIC_API_KEY
python -m jobhunt profile --resume resume.pdf
```

PDFs go over as a base64 document block (Anthropic and Gemini both read them
natively — no OCR, no text extraction library). `.txt` and `.md` also work and
are the fallback for providers that can't take documents.

This writes `profile.json`. It's gitignored — read it, fix anything the model
got wrong, and keep it out of version control.

### 4. Run it

```bash
python -m jobhunt run                    # build the digest
python -m jobhunt run --send             # ...and email it
python -m jobhunt run --limit 10         # cost guard while tuning
python -m jobhunt run --no-draft         # screen only, skip the expensive pass
```

---

## Picking providers

Screening reads hundreds of jobs and wants the cheapest decent model. Drafting
runs ~5 times and wants the best one. So they're configured separately:

```bash
LLM_PROVIDER=anthropic          # sets both stages
SCREEN_PROVIDER=groq            # ...override per stage
DRAFT_PROVIDER=anthropic
SCREEN_MODEL=claude-haiku-4-5-20251001
DRAFT_MODEL=claude-sonnet-5
```

| Provider | Value | Key | PDF resumes | Notes |
|---|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | yes | default; uses the official SDK |
| Google Gemini | `gemini` | `GEMINI_API_KEY` | yes | generous free tier |
| Groq | `groq` | `GROQ_API_KEY` | no | very fast, free tier |
| Grok (xAI) | `xai` (or `grok`) | `XAI_API_KEY` | via local text extraction | OpenAI-compatible at `api.x.ai/v1`; defaults `grok-4.3` (screen) / `grok-4.7` (draft); `XAI_BASE_URL` to override |
| OpenAI-compatible | `openai-compatible` | `GROQ_API_KEY` + `LLM_BASE_URL` | no | Together, OpenRouter, vLLM |
| Ollama | `ollama` | none | no | fully local, `OLLAMA_HOST` |

Providers that cannot read a PDF directly (Groq, Grok, Ollama) get the resume's
text extracted locally with `pypdf` instead. A scanned, image-only PDF has no text
to extract; upload a `.txt`/`.md` version or use Gemini/Anthropic for that.

Everything except Anthropic goes over plain `requests`, so you can delete the
`anthropic` line from `requirements.txt` and still run the whole thing.

Adding a provider is one class in [`jobhunt/providers.py`](jobhunt/providers.py)
with a `complete()` method, plus an entry in the `PROVIDERS` dict.

---

## Tracking

`seen.json` is both the dedupe index and the application tracker — a job you've
already been shown is never shown again. It's gitignored: it's yours, and
shipping one would break the first run for anyone who cloned the repo.

```bash
python -m jobhunt applied "greenhouse:stripe:5501001"   # id is in the digest
python -m jobhunt stats                                 # + CSV export
```

`out/tracker.csv` opens in any spreadsheet.

---

## Scheduling

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs it at 06:00 IST
on weekdays. `seen.json` is carried between runs with `actions/cache`, not
committed — it's personal, and a `seen.json` in the repo would mark every job as
already-seen for anyone who cloned it. Nothing personal ever enters git.

Repository **secrets** to set (Settings → Secrets and variables → Actions):

| Secret | What |
|---|---|
| `PROFILE_JSON` | the entire contents of your local `profile.json` |
| `ANTHROPIC_API_KEY` | (or `GEMINI_API_KEY` / `GROQ_API_KEY`) |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + **App Password**, not your login |
| `MAIL_TO` | where the digest goes |

Optional repository **variables**: `LLM_PROVIDER`, `SCREEN_PROVIDER`,
`DRAFT_PROVIDER`, `SCREEN_MODEL`, `DRAFT_MODEL`.

Trigger it by hand first — Actions → *daily job digest* → *Run workflow*, with
`dry_run` ticked to build the digest artifact without emailing.

Gmail needs an [App Password](https://myaccount.google.com/apppasswords); your
normal password stops working once 2FA is on.

---

## HTTP API

The same pipeline is available as a service, so an app can upload a resume and
trigger a search instead of running the CLI.

```bash
pip install -r requirements.txt
JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  uvicorn jobhunt.api.app:app --env-file .env
# interactive docs: http://127.0.0.1:8000/docs  (Authorize: username = email)
```

### Auth

Multi-user: anyone can register, and each user has their own profile, runs and
jobs. Every endpoint except `/health`, `/auth/register` and `/auth/login` needs
`Authorization: Bearer <access_token>`.

| Endpoint | What |
|---|---|
| `POST /auth/register` | JSON `{"email", "password"}` (password 8–128 chars) → `201`; `409` if taken |
| `POST /auth/login` | form fields `username` (the email) + `password` → `access_token`, `idle_timeout_seconds`, `expires_at` |
| `GET /auth/me` | who am I; also a keep-alive |
| `POST /auth/logout` | revokes the session immediately (`204`) |

**Session timeout.** A session ends after 30 minutes with no authenticated
request (`SESSION_IDLE_MINUTES`); every request resets the clock. The token also
has a hard cap of 12 hours (`SESSION_MAX_HOURS`) however active you are. The
idle check is server-side, so logout and expiry take effect at once. Clients
should treat any `401` as "sign in again".

Passwords are stored as salted scrypt hashes. Login errors never say whether the
email exists, and an account locks for `LOCKOUT_MINUTES` (15) after
`MAX_FAILED_LOGINS` (5) wrong passwords (`429` + `Retry-After`).

| Env var | Default | |
|---|---|---|
| `JWT_SECRET` | — | **required**, ≥ 32 chars; without it auth endpoints return `503` |
| `SESSION_IDLE_MINUTES` | 30 | inactivity timeout |
| `SESSION_MAX_HOURS` | 12 | absolute token lifetime |
| `MAX_FAILED_LOGINS` / `LOCKOUT_MINUTES` | 5 / 15 | per-account lockout |
| `ALLOW_REGISTRATION` | true | `false` closes sign-up (`403`) |
| `MAX_CONCURRENT_RUNS` | 3 | server-wide searches at once (`503`) |

If you ran an earlier single-user version, its `data/jobhunt.db` is renamed to
`jobhunt.pre-auth.bak` on first start and a fresh database is created.

### Endpoints

| Endpoint | What |
|---|---|
| `POST /resume` | multipart `file` (.pdf/.txt/.md, ≤ 5 MB) → extracts your profile and keeps the file as a new version. Earlier uploads stay. Identical bytes are not stored twice. A failed extraction stores nothing. At most `MAX_RESUMES_PER_USER` (default 50) per user, then `409` until you delete some. |
| `GET /resume` | download the newest resume (as an attachment, `404` if none) |
| `GET /resumes` | list every upload, newest first: `id`, `filename`, `size`, `sha256`, `uploaded_at`, `is_current` |
| `GET /resumes/{id}`, `DELETE /resumes/{id}` | download or delete one upload (`204`). The profile and jobs stay. |
| `POST /resume/reextract` | re-run extraction on the newest resume, e.g. after changing model. Overwrites the profile, including manual `PUT /profile` corrections. |
| `GET /profile`, `PUT /profile` | read the profile (with `resume`: the newest upload's metadata); correct fields (e.g. `{"years_experience": 3.2}`) |
| `POST /search` | start a run in the background → `202` + `run_id`. Optional body: `{"draft": true, "limit": 50, "companies": ["Acme"]}` |
| `GET /runs/{run_id}`, `GET /runs/latest` | `status` (running / succeeded / failed), `stage`, `counts`, `error` |
| `GET /jobs` | scored matches. Filters: `min_score` (default = `score_threshold`; `0` = all), `company`, `q`, `location`, `status`, `limit`, `offset` |
| `GET /jobs/{job_id}` | full posting plus the drafted application kit |
| `POST /jobs/{job_id}/applied` | mark applied |
| `GET /stats`, `GET /health` | counts; liveness |
| `GET /insights/skills` | which skills the stored postings ask for, as counts and percentages (below) |

```bash
B=http://127.0.0.1:8000
curl -X POST $B/auth/register -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"a long passphrase"}'
TOKEN=$(curl -s -X POST $B/auth/login -d "username=you@example.com" \
  -d "password=a long passphrase" | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"
curl -H "$AUTH" -F file=@resume.pdf $B/resume
curl -H "$AUTH" -X POST $B/search
curl -H "$AUTH" $B/runs/latest
curl -H "$AUTH" "$B/jobs?min_score=7"
```

### Skill insights

`GET /insights/skills` answers "what do the jobs I'm seeing actually ask for?".
It counts skills across the postings your searches stored, so each figure is
`postings mentioning it / postings analysed`, with `sample_size` returned beside
it. There is no LLM and no outside data: a fixed vocabulary of ~90 skills matched
by regex (`jobhunt/skills.py`), so the same postings always give the same numbers.
A posting counts a skill once however often it repeats it.

```bash
curl -H "$AUTH" "$B/insights/skills?role=ai-ml&top=10"
```

Filters: `role` (`ai-ml`, `backend`, `frontend`, `full-stack`, `mobile`, `data`,
`devops-sre`, `software-engineer`, `other`, taken from the job title),
`category`, `min_score` (default `0` = everything stored, not only matches),
`company`, `q`, `location`, `top`. Each skill carries `in_profile`, and `gaps`
lists the in-demand skills your profile does not mention. The sample is only as
broad as your searches and `config.yaml` filters, and it is capped at the newest
2000 postings (`truncated`); a `note` warns when the sample is small.

Guards, since a run spends LLM tokens: one run at a time (`409`), at most one per
`MIN_RUN_INTERVAL_MIN` minutes (default 30, `429` with `Retry-After`), and at most
`MAX_SCREEN_JOBS` new jobs screened per run (default 80). Jobs that were not
scored are not stored, so the next run retries them.

Data lives in SQLite at `$DB_PATH` (default `data/jobhunt.db`, git-ignored). The
uploaded resumes are stored in the same database (every version, per user, as BLOBs), so the
database holds personal data: keep it private and on a persistent volume when hosted.
The filters, companies and Workday terms still come from `config.yaml` and
`companies.yaml`. `seen.json` is not used by the API.

---

## Layout

```
jobhunt/
  fetch.py       Job dataclass, strip_html, 3 pure parsers, Workday fetcher, fetch_all
  prefilter.py   title/location/freshness gate — no LLM, no cost
  providers.py   the swappable provider interface + 5 backends
  llm.py         screen() / draft() / build_profile() / keyword stub
  pipeline.py    fetch -> filter -> screen -> draft as one callable (used by the API)
  api/           FastAPI app (app.py), SQLite storage (db.py), models (schemas.py)
  digest.py      HTML email (inline CSS only — Gmail strips <style>)
  mailer.py      SMTP
  store.py       seen.json dedupe + tracker + CSV export
  mock.py        fixtures in each ATS's native JSON shape
  cli.py         argparse: profile / run / applied / stats
config.yaml      filters, thresholds, paths
companies.yaml   boards to poll
tests/           no network, no key
```

HTTP is kept out of the parsers on purpose. Each `parse_*(slug, company, body)`
takes already-decoded JSON and returns `list[Job]`, which is what makes `--mock`
exercise the real code path instead of a parallel implementation.

Every job gets `job_id = "{ats}:{slug}:{id}"` — globally unique, so the same
role posted on two boards is still two rows, and a re-run never duplicates.

### ATS quirks the parsers handle

- **Greenhouse** — `content` is HTML-entity-escaped HTML. Unescape *before*
  stripping tags and again after, or you ship `&amp;` into the prompt.
- **Lever** — `createdAt` is epoch **milliseconds**. The full JD is split across
  `descriptionPlain` **+** `lists[].text` **+** `lists[].content` **+**
  `additionalPlain`; concatenate all four or you lose the requirements section
  and every job looks unqualified.
- **Ashby** — skip `isListed: false`; those are unpublished drafts.

---

## Tests

```bash
python -m pytest tests -q
```

No network, no API key, no cost. The suite covers:

- each parser against fixtures in its **native** ATS shape
- the two bugs that cost me an evening each: Lever's epoch-ms timestamps
  (fixture dates are generated relative to *now*, never hardcoded, so they
  can't silently age past the freshness gate) and the `\bsde\b` regex
- prefilter rejects the planted junk: wrong seniority, wrong city, wrong
  function, a stale posting, an unlisted Ashby draft
- the LLM layer with the provider stubbed: batching splits at the configured
  size, JD truncation is applied before send, fenced/preamble/object-or-array
  JSON all parse, scores land on the right job when returned out of order, a
  failed batch warns and the run continues, and the draft kit always has every
  key the digest renders

---

## Cost

With ~15 boards, a tight `config.yaml`, Haiku screening and Sonnet drafting,
this lands in the low single-digit rupees per day. The prefilter is what makes
that true: nothing reaches a model until it has already passed title, location
and freshness. Set `SCREEN_PROVIDER=groq` or `gemini` and it's free.

Use `--limit` while tuning filters so a bad regex can't run up a bill.
