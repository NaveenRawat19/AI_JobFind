"""Study plan generation: requirements -> prompt -> LLM -> validated plain-text plan.

The plan is produced two weeks per call so long plans never hit an output-token
limit, then every chunk is parsed against the planner app's strict format and
re-rendered, so what comes back is always well-formed or an error.
"""
from __future__ import annotations

import os
import re
from typing import Callable

from .providers import LLMError, Provider

WEEKS_PER_CALL = 1
DEFAULT_MAX_TOKENS = 4500
MAX_PROBLEMS_SHOWN = 8

FIELD_LABELS = {"tech": "Tech interviews / software"}

WEEK_RE = re.compile(r"^### Week (\d+): (\S.*)$")
DAY_RE = re.compile(r"^\*\*Day (\d+)(?: \(review\))?\*\*$")
TASK_RE = re.compile(r"^- \[ \] \*\*([^*:]+):\*\* (\S.*)$")
LINK_RE = re.compile(r"\]\(([^)]*)\)")
URL_RE = re.compile(r"^https?://\S+$")
FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)


# ------------------------------------------------------------------ prompt --

def plan_days(req: dict) -> int:
    per_unit = {"days": 1, "weeks": 7, "months": 30}[req["duration_unit"]]
    return req["duration_value"] * per_unit


def _is_tech(req: dict) -> bool:
    return req["field"].strip().lower() == "tech" and not (req.get("field_name") or "").strip()


def _field_label(req: dict) -> str:
    name = (req.get("field_name") or "").strip()
    return name or FIELD_LABELS.get(req["field"].strip().lower(), req["field"])


def _hours(h: float) -> str:
    return f"{h:g} hour" + ("" if h == 1 else "s")


def _duration(req: dict, days: int) -> str:
    if req["duration_unit"] == "days":
        return f"{days} days"
    unit = req["duration_unit"][:-1] if req["duration_value"] == 1 else req["duration_unit"]
    return f"{days} days ({req['duration_value']} {unit})"


def _tracks_per_day(tracks: list[str]) -> str:
    n = len(tracks)
    if n == 1:
        return f"Every day is about {tracks[0]}."
    if n == 2:
        return "Each day covers both tracks."
    return ("Each day covers 2 to 3 tracks, rotating so every track appears at "
            "least twice a week.")


def _example(req: dict, tracks: list[str]) -> str:
    review = req["review_days"]
    if _is_tech(req):
        body = """### Week 1: Hashing and windows · networking basics · OOP and SOLID · tokens and prompts

**Day 1**

- [ ] **DSA:** Solve [Group Anagrams](https://leetcode.com/problems/group-anagrams/) in 30 min (hashing), then read the [NeetCode walkthrough](https://neetcode.io/problems/anagram-groups) and write a one-line pattern note
- [ ] **HLD:** Study [networking basics](https://github.com/donnemartin/system-design-primer) (DNS, TCP, HTTP, TLS) and explain in one sentence what happens when you type a URL; skim DDIA [chapter 1](https://dataintensive.net/) for vocabulary
- [ ] **LLD:** Read [OOP pillars](https://www.hellointerview.com/learn/low-level-design/in-a-hurry/oop-concepts) and write a small Python class with encapsulation and polymorphism; compare with [Refactoring Guru's intro](https://refactoring.guru/design-patterns/what-is-pattern)
- [ ] **AI:** Open the [OpenAI tokenizer](https://platform.openai.com/tokenizer) and the [Anthropic prompting guide](https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview); run the same prompt at temperature 0 and 1 and note the difference"""
        review_day = """

**Day 2 (review)**

- [ ] **DSA:** Redo the problem you failed this week and explain the pattern aloud
- [ ] **HLD:** Redo one design from memory on paper and list its three main trade-offs
- [ ] **LLD:** Code the OOP example again without looking, then add one SOLID fix
- [ ] **AI:** Run 10 test prompts through your extraction script and record which ones fail"""
    else:
        a, b = tracks[0], tracks[-1]
        body = f"""### Week 1: Foundations of the first topics

**Day 1**

- [ ] **{a}:** Read [a well-known guide](https://example.com/guide) on one specific topic for 40 min, then write a one-page summary and 3 self-test questions
- [ ] **{b}:** Complete [a practice set](https://example.com/practice) of 10 questions in 40 min and log the ones you got wrong"""
        review_day = f"""

**Day 2 (review)**

- [ ] **{a}:** Redo the questions you got wrong this week and explain each answer aloud"""
    return body + (review_day if review else "")


def build_prompt(req: dict, first_day: int = 1, last_day: int | None = None,
                 prior: str = "") -> str:
    days = plan_days(req)
    last_day = last_day or days
    tracks = req["tracks"]
    minutes = round(req["hours_per_day"] * 60)
    review = req["review_days"]
    tech = _is_tech(req)
    first_week, last_week = (first_day - 1) // 7 + 1, (last_day - 1) // 7 + 1

    about = [f"- Field: {_field_label(req)}",
             f"- Goal: {req['goal']}",
             f"- Current level: {req['level']}"]
    if req["weak_areas"].strip():
        about.append(f"- Weak areas (give these extra time): {req['weak_areas'].strip()}")
    about += [
        f"- Duration: {_duration(req, days)}. Number the days 1 to {days}.",
        f"- Time available: {_hours(req['hours_per_day'])} per day ({minutes} minutes). Split this "
        "across the tracks and state the minutes in each task, so one day adds up to "
        f"about {minutes} minutes.",
        f"- Tracks (subjects): {', '.join(tracks)}",
        f"- {_tracks_per_day(tracks)}",
    ]
    if req["task_styles"]:
        about.append(f"- Preferred ways of studying: {'; '.join(req['task_styles'])}")
    if review:
        about.append("- Every 7th day (Day 7, 14, 21, ...) is a review day: revise the week, redo "
                     "what I got wrong, and take a short test or mock for each track. Label it "
                     "**Day N (review)**.")
    else:
        about.append("- There are no review days: label every day **Day N**.")
    if req["extra"].strip():
        about.append(f"- Other requests: {req['extra'].strip()}")

    resources = req["resources"].strip()
    if resources:
        res = ("RESOURCES\nBase the plan mainly on these resources I chose (links, books, courses, "
               f"notes):\n{resources}\nUse a link exactly as I wrote it. If you cannot open a link, "
               "still use it as the main reference and do not invent deeper URLs under it. Spread "
               "these resources across the plan in a sensible order.\n")
    else:
        res = "RESOURCES\nI have not chosen any resources, so pick well-known ones for every task.\n"
    extra_sources = ("such as LeetCode or NeetCode problems, System Design Primer, ByteByteGo, "
                     "Hello Interview, Refactoring Guru, the book Designing Data-Intensive "
                     "Applications, and official Anthropic, OpenAI and Google docs" if tech
                     else "from reputable, widely used sources for this field")
    free = " I can only use free material: do not recommend paid courses, books or subscriptions." \
        if req["free_only"] else ""
    res += (f"You may add other well-known sources where my list has gaps, {extra_sources}.{free}\n"
            "Only use real, well-known URLs and prefer stable pages (a section, guide index, docs "
            "page or problem page). If you are not sure a URL exists, leave the link out and say "
            "in words where to find the material.")

    day_line = "2. Start each day with a line on its own: **Day N**" + (
        "   (review days: **Day N (review)**)" if review else "")
    structure = ["Build up gradually: fundamentals first, then depth and practice, then "
                 "full-length tests, mocks or projects, finishing with gaps and revision."]
    if review:
        structure.append("Every 7th day (Day 7, 14, 21, ...) is a review day: revise the week, "
                         "redo what I got wrong, and take a short test or mock for each track. "
                         "Label it **Day N (review)**.")
    structure += ["Where natural, connect tracks within a week.",
                  "Give each week a title that names its focus."]

    parts = [
        "Create a complete day-by-day study plan as a plain text file that I will import into my "
        "planner app. Reply with ONLY the file contents inside one code block, no commentary.",
        "ABOUT ME\n" + "\n".join(about),
        res,
        """WHAT EACH TASK MUST CONTAIN
Each task is one line that tells me exactly what to do in the time I have, not just a topic name. Include:
1. A specific topic, chapter, problem or question (not just a subject name).
2. A concrete action and outcome, with minutes (for example: solve in 30 min and write a pattern note; read the chapter and make one page of notes; write a 150-word answer and self-check it; solve 20 questions and log the wrong ones).
3. Two or three study links of different kinds where they exist: the exercise or question, a primary explanation, and optionally a video or deeper reading.""",
        "PLAN STRUCTURE\n" + "\n".join(f"- {s}" for s in structure),
        f"""STRICT FORMAT RULES (the app rejects anything else)
1. Start each week with a line: ### Week N: short title
   Weeks are numbered 1, 2, 3, ... with no gaps.
{day_line}
   Day numbers run 1, 2, 3, ... to {days} continuously across ALL weeks (Week 2 starts at Day 8 if Week 1 has 7 days). Never restart at 1.
3. Under each day write one task per line, exactly like this:
   - [ ] **TRACK:** task text
   TRACK must be one of: {', '.join(tracks)}. Write each name identically on every day. Every day needs at least one task. Always include the "- [ ] " checkbox and the bold TRACK name followed by a colon.
4. Links are Markdown: [label](https://full-url). Only http:// or https://. Put the link inside the sentence; the label becomes the button text.
5. Keep every task on a single line. No nested bullets, tables or blank lines between a day's tasks. No other headings or text.""",
        "EXAMPLE (format and level of detail only: use MY tracks, topics and duration, not the "
        "example's; it is shortened, a real plan has 7 days per week)\n" + _example(req, tracks),
    ]
    if (first_day, last_day) != (1, days):
        scope = (f"THIS REPLY COVERS ONLY: Day {first_day} to Day {last_day} (Week {first_week} to "
                 f"Week {last_week}). The whole plan runs Day 1 to Day {days}; pace this part as "
                 f"one stretch of that whole. Do not write any other days. Start with the "
                 f"### Week {first_week}: heading and stop after the last task of Day {last_day}.")
        parts.append(scope)
        if prior:
            parts.append("ALREADY WRITTEN (do not repeat any of it; carry on from where it ends "
                         "and do not reuse the same problems or chapters):\n" + prior)
    return "\n\n".join(parts) + "\n"


# ------------------------------------------------------- parsing / rendering --

def _strip_fence(text: str) -> str:
    text = text.strip()
    m = FENCE_RE.match(text)
    if m:
        return m.group(1)
    if text.startswith("```"):                      # opened but never closed
        return text.split("\n", 1)[1] if "\n" in text else ""
    return text


def parse_chunk(text: str, tracks: list[str], first_day: int, last_day: int
                ) -> tuple[list[dict], list[str]]:
    """Returns (weeks, problems). weeks: [{n, title, days: [{n, tasks: [(track, text)]}]}]"""
    canon = {t.lower(): t for t in tracks}
    weeks: list[dict] = []
    problems: list[str] = []
    day: dict | None = None

    for raw in _strip_fence(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := WEEK_RE.match(line):
            weeks.append({"n": int(m.group(1)), "title": m.group(2).strip(), "days": []})
            day = None
        elif m := DAY_RE.match(line):
            if not weeks:
                problems.append(f"{line!r} appears before any '### Week N: title' line")
                continue
            day = {"n": int(m.group(1)), "tasks": []}
            weeks[-1]["days"].append(day)
        elif m := TASK_RE.match(line):
            track = canon.get(m.group(1).strip().lower())
            if day is None:
                problems.append(f"task before any day heading: {line[:60]!r}")
            elif track is None:
                problems.append(f"TRACK {m.group(1)!r} is not one of {', '.join(tracks)}")
            else:
                bad = [u for u in LINK_RE.findall(m.group(2)) if not URL_RE.match(u)]
                if bad:
                    problems.append(f"Day {day['n']}: link {bad[0][:40]!r} is not an http(s) URL")
                day["tasks"].append((track, m.group(2).strip()))
        else:
            problems.append(f"line not in the required format: {line[:70]!r}")

    numbers = [d["n"] for w in weeks for d in w["days"]]
    if numbers != list(range(first_day, last_day + 1)):
        problems.append(f"days must run {first_day} to {last_day} in order, got "
                        f"{numbers[:3]}...{numbers[-2:]} ({len(numbers)} days)")
    for w in weeks:
        if not w["days"]:
            problems.append(f"Week {w['n']} has no days")
        elif (w["days"][0]["n"] - 1) % 7 != 0 or any(
                (d["n"] - 1) // 7 + 1 != w["n"] for d in w["days"]):
            problems.append(f"Week {w['n']} heading does not match its days: every week has 7 "
                            "days, with a new week heading before Day 1, 8, 15, ...")
    for w in weeks:
        for d in w["days"]:
            if not d["tasks"]:
                problems.append(f"Day {d['n']} has no tasks")
    return weeks, problems


def render_weeks(weeks: list[dict], review_days: bool) -> str:
    out: list[str] = []
    for w in weeks:
        out += [f"### Week {w['n']}: {w['title']}", ""]
        for d in w["days"]:
            label = f"**Day {d['n']} (review)**" if review_days and d["n"] % 7 == 0 \
                else f"**Day {d['n']}**"
            out += [label, ""]
            out += [f"- [ ] **{track}:** {task}" for track, task in d["tasks"]]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------- generation --

def _max_tokens() -> int:
    """Output budget for one week. Providers that count prompt + max_tokens against a
    per-minute quota (Groq free tier: 8000) reject a larger budget outright, so keep it
    modest; raise PLAN_MAX_TOKENS for models that spend many tokens on hidden reasoning."""
    try:
        return max(1000, int(os.getenv("PLAN_MAX_TOKENS", DEFAULT_MAX_TOKENS)))
    except ValueError:
        return DEFAULT_MAX_TOKENS


def _chunks(days: int) -> list[tuple[int, int]]:
    step = WEEKS_PER_CALL * 7
    return [(a, min(a + step - 1, days)) for a in range(1, days + 1, step)]


def _context(done: list[dict], review_days: bool) -> str:
    """Compact on purpose: small-quota providers count every prompt token."""
    titles = "\n".join(f"- Week {w['n']}: {w['title']}" for w in done)
    topics = "\n".join(
        f"Day {d['n']} {track}: {LINK_RE.sub('', task).replace('[', '').replace(']', '')[:90]}"
        for d in done[-1]["days"] for track, task in d["tasks"])
    return f"Week titles so far:\n{titles}\n\nTopics covered in the last week (shortened):\n{topics}"


def generate_plan(req: dict, provider: Provider, model: str,
                  progress: Callable[[int, int], None] | None = None) -> str:
    """Plain-text plan for `req`. Raises LLMError if the model cannot produce a valid one."""
    days = plan_days(req)
    tracks, review = req["tracks"], req["review_days"]
    done: list[dict] = []
    chunks = _chunks(days)

    for i, (first, last) in enumerate(chunks):
        prompt = build_prompt(req, first, last, _context(done, review) if done else "")
        weeks, problems = [], []
        for attempt in range(2):
            ask = prompt if not problems else (
                prompt + "\nYour previous reply was rejected for these problems:\n"
                + "\n".join(f"- {p}" for p in problems[:MAX_PROBLEMS_SHOWN])
                + f"\nReply again with the complete, corrected file for Day {first} to Day {last}.\n")
            raw = provider.complete(model, "", ask, _max_tokens())
            weeks, problems = parse_chunk(raw, tracks, first, last)
            if not problems:
                break
        if problems:
            raise LLMError("the model's plan did not match the required format: "
                           + "; ".join(problems[:MAX_PROBLEMS_SHOWN]))
        done += weeks
        if progress:
            progress(i + 1, len(chunks))
    return render_weeks(done, review)
