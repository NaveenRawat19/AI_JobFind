# Prompt for Claude Code (VS Code)

Paste everything below the line into Claude Code. If you unzipped
`jobhunt-starter.zip` into the folder first, it will pick up the existing
code and continue from there instead of starting over.

---

I'm building a personal job-search agent in Python. I'm a software engineer
based in Bangalore, and I'm also going to demo this on my YouTube/Instagram
channel for engineering students — so the code needs to be clean, readable,
and runnable by someone who just cloned it.

## What it does

One daily run:

1. **Fetch** — pull open postings from public ATS APIs (no auth, no scraping)
2. **Prefilter** — deterministic regex/location/freshness gate, no LLM
3. **Screen** — cheap LLM pass scores each surviving job 0–10 against my resume
4. **Draft** — expensive LLM pass writes an application kit for the top ~5
5. **Digest** — build an HTML email and send it to me
6. **Track** — record everything in a JSON store + CSV export

**It must never auto-submit an application.** No LinkedIn or Naukri scraping
either — no public API, and it violates their ToS. The human presses submit;
the agent just does the finding, matching and drafting.

## Data sources (exact endpoints)

    greenhouse  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
    lever       GET https://api.lever.co/v0/postings/{slug}?mode=json
    ashby       GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Field mapping, since each is shaped differently:

- **Greenhouse** → `jobs[]`, with `id`, `title`, `location.name`,
  `absolute_url`, `updated_at`, `content` (HTML-entity-escaped HTML —
  needs `html.unescape` *before* tag stripping, and again after)
- **Lever** → top-level array, with `id`, `text` (the title),
  `categories.location`, `hostedUrl`, `descriptionPlain`, `createdAt`
  (epoch **milliseconds**). The full JD is split across `descriptionPlain`
  **plus** the `lists[]` array (`lists[].text` + `lists[].content`) plus
  `additionalPlain` — concatenate all of them or you lose the requirements.
- **Ashby** → `jobs[]`, with `id`, `title`, `location`, `jobUrl`,
  `descriptionPlain` (fall back to `descriptionHtml`), `publishedAt`,
  `compensation.compensationTierSummary`. **Skip anything with
  `isListed: false`** — those are drafts.

Normalize all three into one dataclass with a globally unique
`job_id = "{ats}:{slug}:{id}"` for dedupe.

## Architecture

Keep HTTP separate from parsing — each ATS gets a pure
`parse_x(slug, company, body) -> list[Job]` function that takes already-decoded
JSON. That's what makes offline testing possible.

    jobhunt/
      fetch.py       Job dataclass, strip_html, 3 parsers, fetch_all
      prefilter.py   title include/exclude regex, location, max_age_days
      llm.py         provider-agnostic screen() + draft() + build_profile()
      digest.py      HTML email (inline CSS only — Gmail strips <style>)
      mailer.py      SMTP
      store.py       seen.json dedupe + application tracker + CSV export
      mock.py        fixtures in each ATS's native shape
      cli.py         argparse: profile / run / applied / stats
    config.yaml      filters, thresholds, file paths
    companies.yaml   list of {ats, slug, name}

## LLM layer

Two stages, because cost lives here:

- **Screen** — batch ~8 jobs per call, truncate each JD to ~1400 chars, return
  a JSON array of `{job_id, score, reason}`. Use the cheapest decent model.
  The system prompt must tell it to be *strict*: penalise seniority mismatch
  hard (a 3-year engineer should not score 8 on a Staff role) and penalise
  hard requirements the candidate plainly lacks.
- **Draft** — only for jobs above the score threshold. Send ~6000 chars of JD,
  return `{fit_summary, tailored_bullets[], gaps[], cover_note,
  questions_to_ask[]}`. The prompt must forbid inventing experience that isn't
  in the profile, and the cover note should be 120–160 words with no
  "I am writing to express my interest" filler.

**Make the provider swappable** — put the client behind one small interface so
I can point screening at Gemini/Groq and drafting at Claude, or run everything
on a free tier. Read model IDs and provider from env vars.

Models I'm considering:
- Anthropic: `claude-haiku-4-5-20251001` (screen), `claude-sonnet-5` (draft)
- Free-tier alternatives: Gemini Flash, Groq Llama, or local Ollama

Also add `build_profile()` that turns my resume into `profile.json`. It should
accept a PDF via base64 document block (Anthropic and Gemini both support this)
and fall back to plain text.

Model replies are unreliable JSON, so write one tolerant parser that handles
markdown fences, a preamble before the JSON, and object-or-array — and make a
failed batch log a warning and continue rather than kill the run.

## Testing (important — I need to demo this)

- `--mock` flag runs the fixtures through the **real parsers**, no network
- `--scorer keyword` is a dumb token-overlap stand-in so the whole pipeline
  runs with **no API key at all** — label it clearly as dev-only
- a test that stubs the LLM client and asserts: batching splits correctly,
  JD truncation is applied, fenced/preamble JSON parses, scores land on the
  right jobs, and the draft kit has all its keys
- fixtures must include junk that *should* get rejected: wrong seniority,
  wrong city, wrong function, a stale posting, an `isListed: false` draft

Two gotchas I already hit — don't repeat them:
1. Lever `createdAt` is epoch **ms**, and my fixture dates were a year stale,
   which silently nuked everything through the freshness filter.
2. `"sde"` as a bare regex does **not** match "Software Development Engineer".
   Use `\bsde\b` and list the spelled-out variants separately.

## Config

`config.yaml` holds `include_titles` / `exclude_titles` (regex lists),
`locations`, `allow_remote`, `max_age_days`, `score_threshold`,
`max_per_digest`, `screen_batch_size`, and all file paths.

## Deliverables

1. The working package above
2. `requirements.txt`, `.env.example`, `.gitignore`, `README.md`
3. A GitHub Actions workflow that runs it every weekday at 06:00 IST, with
   secrets for the API key and SMTP creds, and `seen.json` committed back to
   the repo (or cached) so dedupe survives across runs

Start by getting `python -m jobhunt run --mock --scorer keyword` green
end-to-end, then wire the real LLM calls, then the scheduler. Show me the
prefilter counts at each stage as it runs — I want to see "2000 → 40 → 5" on
screen for the video.
