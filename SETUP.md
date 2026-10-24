# Setup guide

Start here if you've never run a Python project before. Every step assumes you
have nothing installed.

**Time:** about 20 minutes. **Cost:** ₹0 — the free tier of any provider covers
this comfortably, because a full day of running is only ~10 API calls.

**What you end up with:** an email every weekday morning with the 5 jobs worth
your time, each with tailored resume bullets, honest gaps, and a draft cover
note. You read it, edit it, and apply yourself. The tool never submits anything.

---

## Step 1 — Install Python

You need **Python 3.10 or newer**.

**Windows** — download from [python.org/downloads](https://www.python.org/downloads/).
On the first screen of the installer, **tick "Add python.exe to PATH"** before
clicking Install. Missing that checkbox is the single most common reason
nothing works afterwards.

**macOS** — `brew install python` if you have Homebrew, otherwise python.org.

**Linux** — `sudo apt install python3 python3-venv python3-pip`

Check it worked. Open a fresh terminal (PowerShell on Windows, Terminal on
Mac/Linux) and run:

```bash
python --version        # Windows
python3 --version       # macOS / Linux
```

You should see `Python 3.10.x` or higher. If you see "command not found",
reinstall with the PATH box ticked.

> From here on, wherever you see `python`, use `python3` on macOS/Linux.

---

## Step 2 — Get the code

**If you just want to try it on your laptop:** click the green **Code** button
on the repo → **Download ZIP** → unzip it. Or, if you have git:

```bash
git clone https://github.com/<owner>/jobhunt.git
cd jobhunt
```

**If you want it to email you automatically every morning** (this is the whole
point of the project), click **Fork** at the top-right of the repo *first*, then
clone your own fork instead. Step 12 needs it.

---

## Step 3 — Create a virtual environment and install

A virtual environment keeps this project's packages separate from the rest of
your system. Always do this.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell refuses with *"running scripts is disabled on this system"*, run
this once in the same window and try again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

If you get `No module named pip`, run `python -m ensurepip --upgrade` and retry.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You'll know it worked when your prompt starts with `(.venv)`. You need to
re-activate it every time you open a new terminal.

---

## Step 4 — Prove it works, with no API key at all

Before touching keys or resumes, check your install:

```bash
python -m jobhunt run --mock --scorer keyword
```

`--mock` uses bundled fake job postings instead of the network, and
`--scorer keyword` swaps the AI for a dumb word-matching stub. You should see:

```
[2/5] filtering
  prefilter: 12 -> 5 (dropped title=5 location=1 stale=1)
[3/5] screening 5 jobs (keyword stub — DEV ONLY)
  3 scored >= 7.0
[5/5] digest
  wrote out/digest.html

funnel: 12 scanned -> 5 passed filters -> 5 new -> 3 in digest
```

Open `out/digest.html` in your browser. That's the shape of the email you'll get.

**If this works, your setup is fine** and every problem from here is
configuration, not installation.

> The keyword scorer is for testing only. It has no idea what the words mean —
> it can't tell a Staff role from a fresher one. Never judge real jobs by it.

---

## Step 5 — Get a free AI key

Pick one. **Gemini is the best free choice** because it's the only free option
that reads PDF resumes directly.

### Gemini (recommended, free, no card)

1. Go to **[aistudio.google.com/apikey](https://aistudio.google.com/apikey)**
2. Sign in with any Google account
3. Click **Create API key** → pick or create a project
4. Copy the key

> **A Gemini subscription is not an API key.** Google One AI Premium / Gemini
> Advanced is the consumer chat app — a completely separate product with
> separate billing. Paying for it gives you no API access. You need Google AI
> Studio, which is free.

### Other options

| Provider | Where | Card? | Reads PDF resumes |
|---|---|---|---|
| **Groq** | console.groq.com/keys | no | no — export resume to `.txt` |
| **Ollama** (runs on your laptop) | ollama.com | no | no — export resume to `.txt` |
| **Anthropic** | console.anthropic.com | yes, prepaid | yes |

---

## Step 6 — Create your `.env`

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

Open `.env` in any text editor and set:

```bash
LLM_PROVIDER=gemini
GEMINI_API_KEY=paste-your-key-here
```

Now check which models your key can actually use — model names change often,
and a stale name gives you a confusing 404:

```bash
python -c "import os,sys,requests; sys.path.insert(0,'.'); from jobhunt.cli import _load_env; _load_env(); print('\n'.join(m['name'].replace('models/','') for m in requests.get('https://generativelanguage.googleapis.com/v1beta/models', params={'key':os.environ['GEMINI_API_KEY']}).json()['models'] if 'generateContent' in m.get('supportedGenerationMethods',[])))"
```

Pick a fast/cheap model for screening and a stronger one for drafting, and put
them in `.env`:

```bash
SCREEN_MODEL=gemini-3.5-flash-lite
DRAFT_MODEL=gemini-3.6-flash
```

Screening reads every surviving job, so it wants cheap. Drafting runs about five
times a day, so it can afford quality. If a name above 404s, use one from the
list your key printed.

> `.env` is already in `.gitignore`. Never commit it, never paste your key into
> a chat, a screenshot, or a video. If you do, regenerate it immediately.

---

## Step 7 — Turn your resume into a profile

Put your resume in the project folder, then:

```bash
python -m jobhunt profile --resume resume.pdf
```

This writes `profile.json`. **Open it and fix anything wrong** — this file is
what every job gets scored against, so an error here quietly skews every result.
Check especially:

- `years_experience` — if this is wrong, every seniority judgement is wrong
- `target_titles` — the roles you actually want
- `core_skills` — drop anything you'd be embarrassed to be interviewed on

On Groq or Ollama, export your resume to `.txt` first and pass that instead.

---

## Step 8 — Tune your filters (the most important step)

Open `config.yaml`. This is a plain regex and location gate that runs **before**
any AI call, and it's what makes the project nearly free: it takes ~3000 postings
down to ~20 for zero cost, so the AI only ever reads jobs that already fit.

Set three things:

```yaml
filters:
  include_titles:     # a job must match at least one of these
    - 'software engineer'
    - '\bsde\b'
  exclude_titles:     # ...and none of these
    - '\b(staff|principal|senior)\b'    # drop levels you can't reach yet
    - '\b(sales|marketing|recruit)\b'
  locations:          # matched against location + title
    - bangalore
    - bengaluru
  allow_remote: true
  max_age_days: 30
score_threshold: 7.0  # below this, no draft and no digest slot
max_per_digest: 5
```

Two traps worth knowing:

- **`sde` does not match "Software Development Engineer".** They share no
  letters in that order. You need `\bsde\b` for the acronym *and*
  `software development engineer` spelled out as a separate line, or you'll
  silently miss half of all postings.
- **Exclude the levels above you.** If you have 2 years and don't exclude
  `senior`, most of your AI budget goes to rejecting senior roles one at a time.
  A regex can do that for free.

---

## Step 9 — Pick your companies

Open `companies.yaml`. The `slug` is the last part of a company's public careers
board URL:

| Careers board URL | `ats` | `slug` |
|---|---|---|
| `boards.greenhouse.io/stripe` | `greenhouse` | `stripe` |
| `jobs.lever.co/netlify` | `lever` | `netlify` |
| `jobs.ashbyhq.com/ramp` | `ashby` | `ramp` |

To find a company's board, search *"<company> careers greenhouse"* (or lever /
ashby). If none of the three work, that company uses a different system and this
tool can't read it — that's fine, move on.

Start with **10–15 companies you would actually join**. A list of 200 is noise.

A dead slug prints `HTTP 404` and the run continues, so a wrong entry never
breaks anything. But watch the job counts printed per board — a board reporting
nothing every day is a slug you need to fix.

> **No LinkedIn or Naukri.** Neither has a public API, and scraping them
> violates their terms of service. Don't add it.

---

## Step 10 — Your first real run

Use `--limit` the first time so a bad filter can't burn through your quota:

```bash
python -m jobhunt run --limit 10
```

Then open **`out/tracker.csv`** and read the `score` and `reason` columns. This
is your tuning feedback loop:

| What you see | What it means | Fix |
|---|---|---|
| `prefilter: 3000 -> 0` | filters too tight | loosen `include_titles`, add locations, raise `max_age_days` |
| Everything scores 3–4 | filters too loose — wrong jobs are reaching the AI | tighten `exclude_titles`, especially seniority |
| A few 7–9s | working | drop `--limit` and run for real |

Once the reasons look sensible:

```bash
python -m jobhunt run
```

Open `out/digest.html`.

---

## Step 11 — Email it to yourself

Gmail needs an **App Password** — your normal password will not work once
two-factor auth is on.

1. Turn on 2-Step Verification on your Google account
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create one, copy the 16 characters

Add to `.env`:

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your-16-char-app-password
MAIL_TO=you@gmail.com
```

Then:

```bash
python -m jobhunt run --send
```

---

## Step 12 — Make it run itself every morning

This is the part that turns it from a script into an agent. GitHub will run it
for you on a schedule, for free, with your laptop closed.

1. Push your `config.yaml` and `companies.yaml` changes to your fork.
   **`.env`, `profile.json`, your resume and `seen.json` are all gitignored and
   will not be pushed** — that's deliberate. Nothing personal enters git, so a
   public fork is fine. Your key and profile go in as secrets in the next step,
   and `seen.json` is carried between runs by GitHub's cache.

2. In your fork: **Settings → Secrets and variables → Actions**.

   Add these under **Secrets**:

   | Secret | Value |
   |---|---|
   | `PROFILE_JSON` | the entire contents of your local `profile.json`, pasted |
   | `GEMINI_API_KEY` | your key (or `GROQ_API_KEY` / `ANTHROPIC_API_KEY`) |
   | `SMTP_USER` | your Gmail address |
   | `SMTP_PASS` | your 16-character App Password |
   | `MAIL_TO` | where the digest should go |

   And these under **Variables**:

   | Variable | Value |
   |---|---|
   | `LLM_PROVIDER` | `gemini` |
   | `SCREEN_MODEL` | `gemini-3.5-flash-lite` |
   | `DRAFT_MODEL` | `gemini-3.6-flash` |

3. Go to the **Actions** tab and enable workflows (forks start with them off).

4. Test it now instead of waiting for tomorrow: **Actions → daily job digest →
   Run workflow**, tick **dry_run**, and run it. That builds the digest and
   uploads it as a downloadable artifact without emailing anyone.

5. If that's green, you're done. It runs at **06:00 IST every weekday**.

To change the time, edit the `cron` line in `.github/workflows/daily.yml`. It's
in UTC, so subtract 5 hours 30 minutes from your intended IST time.

---

## Step 13 — Using it day to day

When you apply to something, mark it so you can track your funnel:

```bash
python -m jobhunt applied "greenhouse:cloudflare:7462799"   # the id is in the digest
python -m jobhunt stats
```

`out/tracker.csv` opens in Excel or Google Sheets.

`seen.json` means you're never shown the same job twice. Don't delete it unless
you want to start over. It's gitignored, so it stays on your machine — and in
GitHub Actions it's kept in the cache between runs. If you pause the schedule
for more than a week the cache expires and you'll be re-shown some older jobs
once; that's the only downside, and it's harmless.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `python: command not found` | PATH box unticked during install | reinstall Python, tick "Add to PATH" |
| `No module named pip` | incomplete install | `python -m ensurepip --upgrade` |
| PowerShell: "running scripts is disabled" | default execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `No module named jobhunt` | wrong folder, or venv not active | `cd` into the project root, re-activate `.venv` |
| `GEMINI_API_KEY is not set` | `.env` missing, or the key line is blank | check `.env` sits next to `config.yaml` |
| `gemini HTTP 404` | model name retired | re-run the model-list command in Step 6 |
| `gemini stopped early (finishReason=MAX_TOKENS)` | reasoning models spend output tokens thinking | raise the ceilings in `jobhunt/llm.py` |
| `prefilter: 3000 -> 0` | filters too tight | loosen `include_titles` / `locations` |
| `HTTP 404` next to a company | dead slug, company changed ATS | fix or delete it in `companies.yaml` |
| SMTP `Username and Password not accepted` | using your Google password | use a 16-character App Password |
| Digest arrives empty | nothing cleared `score_threshold` | lower it to 6.0, or loosen filters |

---

## Ground rules

- **Never commit `.env`.** If a key ever lands in a screenshot, a video frame,
  or a chat message, regenerate it immediately — it takes 10 seconds.
- **Read before you send.** The cover notes are drafts written by a model that
  has only seen your `profile.json`. Check every claim is actually true about
  you before it goes to a human.
- **The tool never applies for you, by design.** Auto-submitting applications is
  how you get blocked by ATS vendors and how a hiring manager gets 40 identical
  letters. The agent does the finding, matching and drafting. You do the deciding.
