"""Study plan: prompt building, format validation, chunked generation and the endpoint."""
from __future__ import annotations

import re

import pytest
from test_api import client, h, signup  # noqa: F401  (fixtures)

from jobhunt import studyplan
from jobhunt.api import app as app_module
from jobhunt.providers import LLMError

REQ = {
    "field": "tech", "field_name": None, "goal": "Prepare for Backend Interviews",
    "level": "intermediate", "weak_areas": "", "duration_value": 8, "duration_unit": "weeks",
    "hours_per_day": 2, "review_days": True,
    "tracks": ["DSA", "HLD", "LLD", "AI", "Backend", "Databases"], "track_mode": "all",
    "resources": ("LLD: https://codefarm.in/roadmap/lld,  "
                  "https://www.hellointerview.com/learn/low-level-design/\n"
                  "HLD: https://codefarm.in/roadmap/system-design"),
    "free_only": False,
    "task_styles": ["Practice problems or questions", "Reading chapters or articles",
                    "Projects or hands-on work"],
    "extra": "",
}


def fake_chunk(first: int, last: int, tracks: list[str], review: bool = True) -> str:
    out = []
    for d in range(first, last + 1):
        if (d - 1) % 7 == 0:
            out += [f"### Week {(d - 1) // 7 + 1}: Focus {d}", ""]
        out += [f"**Day {d} (review)**" if review and d % 7 == 0 else f"**Day {d}**", ""]
        out += [f"- [ ] **{t}:** Study topic {d} for 40 min with [guide](https://example.com/{d})"
                for t in tracks[:3]]
        out.append("")
    return "```text\n" + "\n".join(out) + "\n```"


class FakeProvider:
    """Answers each prompt with a valid plan for the day range the prompt asks for."""

    def __init__(self, review: bool = True, break_first: bool = False):
        self.prompts: list[str] = []
        self.review, self.break_first = review, break_first

    def complete(self, model, system, user, max_tokens, json_mode=False):
        self.prompts.append(user)
        if self.break_first and len(self.prompts) == 1:
            return "Sure! Here is your plan"
        m = re.search(r"COVERS ONLY: Day (\d+) to Day (\d+)", user)
        first, last = (int(m[1]), int(m[2])) if m else (1, int(re.search(r"to (\d+)\.", user)[1]))
        return fake_chunk(first, last, REQ["tracks"], self.review)


# ------------------------------------------------------------------ prompt --

def test_prompt_carries_the_requirements():
    p = studyplan.build_prompt(REQ)
    assert "- Field: Tech interviews / software" in p
    assert "- Goal: Prepare for Backend Interviews" in p
    assert "- Duration: 56 days (8 weeks). Number the days 1 to 56." in p
    assert "2 hours per day (120 minutes)" in p
    assert "- Tracks (subjects): DSA, HLD, LLD, AI, Backend, Databases" in p
    assert "Each day covers 2 to 3 tracks" in p
    assert "Practice problems or questions; Reading chapters or articles; Projects or hands-on work" in p
    assert "HLD: https://codefarm.in/roadmap/system-design" in p
    assert "TRACK must be one of: DSA, HLD, LLD, AI, Backend, Databases." in p
    assert "to 56 continuously" in p
    assert "THIS REPLY COVERS" not in p          # one call for the whole plan


def test_prompt_adapts_to_options():
    p = studyplan.build_prompt({**REQ, "review_days": False, "free_only": True, "tracks": ["Math"],
                                "field": "other", "field_name": "Calculus", "resources": "",
                                "duration_value": 30, "duration_unit": "days", "hours_per_day": 1.5,
                                "weak_areas": "Limits", "extra": "Weekends lighter"})
    assert "- Field: Calculus" in p and "- Duration: 30 days. " in p
    assert "1.5 hours per day (90 minutes)" in p
    assert "no review days" in p and "(review)" not in p.split("EXAMPLE")[0]
    assert "only use free material" in p.lower()
    assert "I have not chosen any resources" in p
    assert "Every day is about Math." in p
    assert "Weak areas (give these extra time): Limits" in p and "Weekends lighter" in p


def test_later_chunks_say_which_days_and_what_was_written():
    p = studyplan.build_prompt(REQ, 15, 28, prior="Week titles so far:\n- Week 1: A")
    assert "COVERS ONLY: Day 15 to Day 28 (Week 3 to Week 4)" in p
    assert "ALREADY WRITTEN" in p and "Week 1: A" in p


@pytest.mark.parametrize("unit,value,days", [("days", 10, 10), ("weeks", 8, 56), ("months", 2, 60)])
def test_plan_days(unit, value, days):
    assert studyplan.plan_days({"duration_unit": unit, "duration_value": value}) == days


# ----------------------------------------------------------------- parsing --

def test_parse_accepts_a_valid_chunk_and_fixes_review_labels():
    weeks, problems = studyplan.parse_chunk(fake_chunk(1, 14, REQ["tracks"], review=False),
                                            REQ["tracks"], 1, 14)
    assert problems == []
    text = studyplan.render_weeks(weeks, review_days=True)
    assert "**Day 7 (review)**" in text and "**Day 14 (review)**" in text
    assert "**Day 6**" in text and "(review)**\n\n- [ ]" in text
    assert text.startswith("### Week 1: Focus 1\n\n**Day 1**\n\n- [ ] **DSA:** ")


@pytest.mark.parametrize("bad,expect", [
    ("### Week 1: T\n\n**Day 1**\n\n- [ ] **Cooking:** x", "not one of"),
    ("### Week 1: T\n\n**Day 1**\n\n- DSA: x", "required format"),
    ("### Week 1: T\n\n**Day 1**\n\n- [ ] **DSA:** [a](javascript:alert(1))", "not an http(s) URL"),
    ("### Week 1: T\n\n**Day 1**", "no tasks"),
    ("**Day 1**\n\n- [ ] **DSA:** x", "before any"),
    ("### Week 1: T\n\n**Day 2**\n\n- [ ] **DSA:** x", "days must run"),
])
def test_parse_rejects_malformed_plans(bad, expect):
    last = 2 if "Day 2" in bad else 1
    _, problems = studyplan.parse_chunk(bad, REQ["tracks"], 1, last)
    assert any(expect in p for p in problems), problems


def test_parse_requires_week_headings_every_seven_days():
    text = fake_chunk(1, 8, REQ["tracks"]).replace("### Week 2: Focus 8\n\n", "")
    _, problems = studyplan.parse_chunk(text, REQ["tracks"], 1, 8)
    assert any("Week 1 heading does not match" in p for p in problems)


# -------------------------------------------------------------- generation --

def test_generate_splits_long_plans_into_one_week_calls():
    prov = FakeProvider()
    plan = studyplan.generate_plan(REQ, prov, "m")
    assert len(prov.prompts) == 8                       # 56 days, one week per call
    assert "ALREADY WRITTEN" in prov.prompts[1] and "ALREADY WRITTEN" not in prov.prompts[0]
    assert plan.count("**Day ") == 56 and plan.count("### Week ") == 8
    assert "**Day 56 (review)**" in plan and "```" not in plan


def test_calls_stay_small_enough_for_a_8000_tpm_quota():
    class Spy(FakeProvider):
        def complete(self, model, system, user, max_tokens, json_mode=False):
            self.budgets.append((len(user) // 3, max_tokens))
            return super().complete(model, system, user, max_tokens)

    prov = Spy()
    prov.budgets = []
    studyplan.generate_plan(REQ, prov, "m")
    assert all(max_tokens == 4500 and prompt + max_tokens < 8000 for prompt, max_tokens in prov.budgets)


def test_generate_handles_a_partial_final_week():
    plan = studyplan.generate_plan({**REQ, "duration_value": 10, "duration_unit": "days"},
                                   FakeProvider(), "m")
    assert plan.count("**Day ") == 10 and plan.count("### Week ") == 2
    assert "**Day 7 (review)**" in plan and "**Day 10**" in plan


def test_generate_retries_once_with_the_problems():
    prov = FakeProvider(break_first=True)
    plan = studyplan.generate_plan({**REQ, "duration_value": 7, "duration_unit": "days"}, prov, "m")
    assert len(prov.prompts) == 2 and "previous reply was rejected" in prov.prompts[1]
    assert plan.count("**Day ") == 7


def test_generate_gives_up_after_a_second_bad_reply():
    class Bad:
        def complete(self, *a, **k):
            return "no plan here"
    with pytest.raises(LLMError, match="required format"):
        studyplan.generate_plan(REQ, Bad(), "m")


# ---------------------------------------------------------------- endpoint --

# ----------------------------------------------------------- rate limiting --

GROQ_429 = ('{"error":{"message":"Rate limit reached ... on tokens per minute (TPM): '
            'Limit 8000, Used 6132, Requested 2032. Please try again in 1.23s. Need more"}}')


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.mark.parametrize("text,headers,expected", [
    (GROQ_429, {}, 1.73),
    ("Please try again in 350ms.", {}, 1.5),
    ("Please try again in 1m12.5s.", {}, 73.0),
    ("", {"retry-after": "7"}, 7.5),
    ("Please try again in 2h10m3s.", {}, None),      # daily quota: not worth waiting
    ("no hint", {}, 5.5),
])
def test_retry_wait(text, headers, expected):
    from jobhunt import providers
    got = providers._retry_wait(Resp(429, text, headers))
    assert got == expected if expected is None else got == pytest.approx(expected)


def test_openai_compatible_waits_out_a_rate_limit(monkeypatch):
    from jobhunt import providers
    replies = [Resp(429, GROQ_429), Resp(429, GROQ_429), Resp(200)]
    sleeps: list[float] = []
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(providers.requests, "post", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(providers.time, "sleep", sleeps.append)
    assert providers.GroqProvider().complete("m", "", "hi", 10) == "ok"
    assert sleeps == [pytest.approx(1.73)] * 2


def test_rate_limit_gives_up_after_the_retries(monkeypatch):
    from jobhunt import providers
    calls: list[int] = []
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(providers.requests, "post", lambda *a, **k: calls.append(1) or Resp(429, GROQ_429))
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    with pytest.raises(LLMError, match="HTTP 429"):
        providers.GroqProvider().complete("m", "", "hi", 10)
    assert len(calls) == providers.RATE_LIMIT_RETRIES + 1


@pytest.fixture
def fake_llm(monkeypatch):
    prov = FakeProvider()
    monkeypatch.setattr(app_module, "resolve", lambda stage, check=True: (prov, "stub-model"))
    return prov


def test_study_plan_is_generated_stored_and_returned(client, h, fake_llm):
    r = client.post("/study-plan", headers=h, json=REQ)
    assert r.status_code == 201, r.text
    body = r.json()
    assert {k: body[k] for k in REQ} == REQ
    assert body["id"] and body["created_at"]
    assert body["plan"].count("**Day ") == 56
    assert client.get("/study-plan", headers=h).json() == body


def test_each_user_has_their_own_latest_plan(client, h, fake_llm):
    assert client.get("/study-plan", headers=h).status_code == 404
    client.post("/study-plan", headers=h, json=REQ)
    newer = client.post("/study-plan", headers=h, json={**REQ, "goal": "Second"}).json()
    assert client.get("/study-plan", headers=h).json()["id"] == newer["id"]
    assert client.get("/study-plan", headers=signup(client, "b@example.com")).status_code == 404


def test_failed_generation_is_502_but_keeps_the_requirements(client, h, monkeypatch):
    class Bad:
        def complete(self, *a, **k):
            raise LLMError("provider down")
    monkeypatch.setattr(app_module, "resolve", lambda stage, check=True: (Bad(), "m"))
    r = client.post("/study-plan", headers=h, json=REQ)
    assert r.status_code == 502 and "provider down" in r.json()["detail"]
    saved = client.get("/study-plan", headers=h).json()
    assert saved["plan"] is None and saved["goal"] == REQ["goal"]


def test_missing_llm_config_is_503_and_stores_nothing(client, h, monkeypatch):
    def no_key(stage, check=True):
        raise LLMError("ANTHROPIC_API_KEY is not set")
    monkeypatch.setattr(app_module, "resolve", no_key)
    assert client.post("/study-plan", headers=h, json=REQ).status_code == 503
    assert client.get("/study-plan", headers=h).status_code == 404


def test_plan_length_is_capped(client, h, fake_llm, monkeypatch):
    monkeypatch.setenv("MAX_PLAN_DAYS", "30")
    r = client.post("/study-plan", headers=h, json=REQ)
    assert r.status_code == 422 and "at most 30 days" in r.json()["detail"]
    assert fake_llm.prompts == []


@pytest.mark.parametrize("patch", [
    {"duration_unit": "years"}, {"duration_value": 0}, {"hours_per_day": 25},
    {"hours_per_day": 0}, {"goal": ""}, {"tracks": []}, {"tracks": "DSA"}, {"unknown": 1},
])
def test_study_plan_validation(client, h, fake_llm, patch):
    assert client.post("/study-plan", headers=h, json={**REQ, **patch}).status_code == 422


def test_study_plan_needs_login(client):
    assert client.post("/study-plan", json=REQ).status_code == 401
