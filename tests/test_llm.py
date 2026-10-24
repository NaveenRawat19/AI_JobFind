"""LLM layer with the provider stubbed out. No key, no network, no cost.

Asserts the four things that actually break in production:
  1. batching splits the way config says it does
  2. JD truncation really is applied before the text goes over the wire
  3. the JSON parser survives fences, preambles and object-or-array replies
  4. scores land on the right job, and a bad batch does not kill the run
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt import llm, providers
from jobhunt.fetch import Job
from jobhunt.providers import LLMError, Provider

PROFILE = {"core_skills": ["Go", "Kubernetes"], "target_titles": ["Backend Engineer"],
           "seniority": "mid", "years_experience": 3}


class StubProvider(Provider):
    """Records every call and replays canned replies in order."""

    name = "stub"

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    def complete(self, model, system, user, max_tokens, json_mode=False):
        self.calls.append({"model": model, "system": system, "user": user,
                           "max_tokens": max_tokens, "json_mode": json_mode})
        if not self.replies:
            return "[]"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    # convenience: the JOBS payload the stage actually sent
    def payload(self, i: int = 0) -> list[dict]:
        return json.loads(self.calls[i]["user"].split("JOBS:\n", 1)[1])


def make_jobs(n: int, desc: str = "Go and Kubernetes at scale.") -> list[Job]:
    return [Job(job_id=f"greenhouse:acme:{i}", ats="greenhouse", company="Acme",
                title=f"Backend Engineer {i}", location="Bangalore",
                url=f"https://example.com/{i}", description=desc)
            for i in range(n)]


def scores_reply(jobs, score=8.0, reason="fits"):
    return json.dumps([{"job_id": j.job_id, "score": score, "reason": reason}
                       for j in jobs])


# ------------------------------------------------------- tolerant JSON -----

@pytest.mark.parametrize("raw,expected", [
    ('[{"a": 1}]', [{"a": 1}]),
    ('```json\n[{"a": 1}]\n```', [{"a": 1}]),
    ('```\n[{"a": 1}]\n```', [{"a": 1}]),
    ('Here is the JSON you asked for:\n[{"a": 1}]', [{"a": 1}]),
    ('[{"a": 1}]\n\nLet me know if you need anything else!', [{"a": 1}]),
    ('```json\nSure —\n{"a": 1}\n```\nDone.', {"a": 1}),
    ('  {"a": 1}  ', {"a": 1}),
])
def test_parse_json_survives_real_model_habits(raw, expected):
    assert llm.parse_json(raw) == expected


@pytest.mark.parametrize("raw", ["", "no json at all", "{broken", None])
def test_parse_json_raises_on_garbage(raw):
    with pytest.raises(ValueError):
        llm.parse_json(raw)


def test_as_list_accepts_array_wrapped_object_and_bare_object():
    assert llm._as_list([{"job_id": "a"}]) == [{"job_id": "a"}]
    assert llm._as_list({"jobs": [{"job_id": "a"}]}) == [{"job_id": "a"}]
    assert llm._as_list({"job_id": "a"}) == [{"job_id": "a"}]


# ------------------------------------------------------------- batching ----

def test_screen_splits_into_batches_of_the_configured_size():
    jobs = make_jobs(20)
    stub = StubProvider([scores_reply(jobs[i:i + 8]) for i in (0, 8, 16)])

    llm.screen(jobs, PROFILE, batch_size=8, provider=stub, model="m")

    assert len(stub.calls) == 3
    assert [len(stub.payload(i)) for i in range(3)] == [8, 8, 4]


def test_screen_makes_one_call_when_everything_fits_in_a_batch():
    jobs = make_jobs(5)
    stub = StubProvider([scores_reply(jobs)])
    llm.screen(jobs, PROFILE, batch_size=8, provider=stub, model="m")
    assert len(stub.calls) == 1


def test_screen_batch_size_zero_does_not_hang():
    jobs = make_jobs(3)
    stub = StubProvider([scores_reply(jobs[i:i + 1]) for i in range(3)])
    llm.screen(jobs, PROFILE, batch_size=0, provider=stub, model="m")
    assert len(stub.calls) == 3


# ----------------------------------------------------------- truncation ----

def test_screen_truncates_the_jd_before_sending():
    jobs = make_jobs(1, desc="x" * 9000)
    stub = StubProvider([scores_reply(jobs)])

    llm.screen(jobs, PROFILE, batch_size=8, jd_chars=1400, provider=stub, model="m")

    assert len(stub.payload(0)[0]["description"]) == 1400
    assert "x" * 1401 not in stub.calls[0]["user"]
    assert jobs[0].description == "x" * 9000   # the Job itself is untouched


def test_draft_truncates_at_a_larger_limit():
    jobs = make_jobs(1, desc="y" * 20000)
    stub = StubProvider(['{"fit_summary":"ok","tailored_bullets":[],"gaps":[],'
                         '"cover_note":"","questions_to_ask":[]}'])

    llm.draft(jobs, PROFILE, jd_chars=6000, provider=stub, model="m")

    body = stub.calls[0]["user"]
    assert "y" * 6000 in body
    assert "y" * 6001 not in body


# ------------------------------------------------------- score plumbing ----

def test_scores_land_on_the_right_jobs_even_when_returned_out_of_order():
    jobs = make_jobs(3)
    stub = StubProvider([json.dumps([
        {"job_id": jobs[2].job_id, "score": 9.5, "reason": "third"},
        {"job_id": jobs[0].job_id, "score": 4.0, "reason": "first"},
        {"job_id": jobs[1].job_id, "score": 7.5, "reason": "second"},
    ])])

    llm.screen(jobs, PROFILE, batch_size=8, provider=stub, model="m")

    assert [j.score for j in jobs] == [4.0, 7.5, 9.5]
    assert [j.reason for j in jobs] == ["first", "second", "third"]


def test_unknown_job_ids_in_the_reply_are_ignored():
    jobs = make_jobs(2)
    stub = StubProvider([json.dumps([
        {"job_id": "greenhouse:acme:0", "score": 8, "reason": "ok"},
        {"job_id": "hallucinated:job:999", "score": 10, "reason": "not real"},
    ])])
    llm.screen(jobs, PROFILE, batch_size=8, provider=stub, model="m")
    assert jobs[0].score == 8.0
    assert jobs[1].score is None


def test_scores_are_clamped_to_the_0_10_range():
    jobs = make_jobs(2)
    stub = StubProvider([json.dumps([
        {"job_id": jobs[0].job_id, "score": 47, "reason": "over"},
        {"job_id": jobs[1].job_id, "score": -3, "reason": "under"},
    ])])
    llm.screen(jobs, PROFILE, batch_size=8, provider=stub, model="m")
    assert [j.score for j in jobs] == [10.0, 0.0]


def test_fenced_reply_with_preamble_still_scores(capsys):
    jobs = make_jobs(1)
    stub = StubProvider([
        "Sure! Here are the scores:\n```json\n"
        f'[{{"job_id": "{jobs[0].job_id}", "score": 8.5, "reason": "strong Go match"}}]'
        "\n```"])
    llm.screen(jobs, PROFILE, batch_size=8, provider=stub, model="m")
    assert jobs[0].score == 8.5
    assert jobs[0].reason == "strong Go match"


# ------------------------------------------------------- failure modes -----

def test_one_bad_batch_warns_and_the_run_continues(capsys):
    jobs = make_jobs(4)
    stub = StubProvider(["I'd rather not answer that.", scores_reply(jobs[2:])])

    llm.screen(jobs, PROFILE, batch_size=2, provider=stub, model="m")

    assert [j.score for j in jobs[:2]] == [None, None]   # batch 1 dropped
    assert [j.score for j in jobs[2:]] == [8.0, 8.0]     # batch 2 survived
    assert "screen batch 1 failed" in capsys.readouterr().out


def test_a_provider_error_does_not_abort_screening():
    jobs = make_jobs(4)
    stub = StubProvider([LLMError("HTTP 429 rate limited"), scores_reply(jobs[2:])])
    llm.screen(jobs, PROFILE, batch_size=2, provider=stub, model="m")
    assert jobs[3].score == 8.0


# ----------------------------------------------------------------- draft ---

def test_draft_kit_has_every_key_the_digest_renders():
    jobs = make_jobs(1)
    stub = StubProvider(["```json\n" + json.dumps({
        "fit_summary": "Two sentences about fit.",
        "tailored_bullets": ["Cut p99 by 70%", "Migrated 40 services"],
        "gaps": ["No Rust in production"],
        "cover_note": "A plain 130-word note.",
        "questions_to_ask": ["How is on-call split?", "What is the deploy cadence?"],
    }) + "\n```"])

    llm.draft(jobs, PROFILE, provider=stub, model="m")

    kit = jobs[0].draft
    assert set(kit) == set(llm.DRAFT_KEYS)
    assert kit["fit_summary"].startswith("Two sentences")
    assert len(kit["tailored_bullets"]) == 2
    assert isinstance(kit["gaps"], list)


def test_draft_fills_missing_keys_rather_than_crashing_the_digest():
    jobs = make_jobs(1)
    stub = StubProvider(['{"fit_summary": "only this one"}'])
    llm.draft(jobs, PROFILE, provider=stub, model="m")
    kit = jobs[0].draft
    assert set(kit) == set(llm.DRAFT_KEYS)
    assert kit["tailored_bullets"] == []
    assert kit["cover_note"] == ""


def test_failed_draft_leaves_a_renderable_empty_kit(capsys):
    jobs = make_jobs(1)
    stub = StubProvider(["not json"])
    llm.draft(jobs, PROFILE, provider=stub, model="m")
    assert set(jobs[0].draft) == set(llm.DRAFT_KEYS)
    assert "draft failed" in capsys.readouterr().out


def test_draft_makes_one_call_per_job():
    jobs = make_jobs(3)
    stub = StubProvider(['{"fit_summary":"a"}'] * 3)
    llm.draft(jobs, PROFILE, provider=stub, model="m")
    assert len(stub.calls) == 3


# ------------------------------------------------- keyword stub (no API) ---

def test_keyword_screen_scores_without_any_provider():
    jobs = make_jobs(2)
    jobs[1].description = "Sales quota and CRM hygiene."
    jobs[1].title = "Account Executive"
    llm.keyword_screen(jobs, PROFILE)
    assert jobs[0].score > jobs[1].score
    assert all(j.reason.startswith("[keyword stub]") for j in jobs)


def test_keyword_screen_stays_in_range_with_an_empty_profile():
    jobs = make_jobs(1)
    llm.keyword_screen(jobs, {})
    assert 0 <= jobs[0].score <= 10


# ------------------------------------------------- provider resolution -----

ENV_KEYS = ["LLM_PROVIDER", "SCREEN_PROVIDER", "DRAFT_PROVIDER",
            "SCREEN_MODEL", "DRAFT_MODEL", "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY", "GROQ_API_KEY"]


@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_stage_provider_overrides_the_global_one(clean_env):
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    clean_env.setenv("SCREEN_PROVIDER", "groq")
    clean_env.setenv("GROQ_API_KEY", "gsk_test")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    screen_p, screen_m = providers.resolve("screen")
    draft_p, draft_m = providers.resolve("draft")

    assert screen_p.name == "groq"
    assert draft_p.name == "anthropic"
    assert draft_m == "claude-sonnet-5"      # per-provider default
    assert screen_m == "llama-3.3-70b-versatile"


def test_explicit_model_wins_over_the_default(clean_env):
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    clean_env.setenv("SCREEN_MODEL", "claude-haiku-4-5-20251001")
    _, model = providers.resolve("screen")
    assert model == "claude-haiku-4-5-20251001"


def test_missing_key_fails_at_resolve_not_on_the_first_batch(clean_env):
    """A key error must surface before we start spending, otherwise every
    batch fails identically and the run ends with a plausible-looking empty
    digest."""
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        providers.resolve("screen")


def test_blank_key_counts_as_missing(clean_env):
    clean_env.setenv("LLM_PROVIDER", "groq")
    clean_env.setenv("GROQ_API_KEY", "   ")
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        providers.resolve("screen")


def test_ollama_needs_no_credentials(clean_env):
    clean_env.setenv("LLM_PROVIDER", "ollama")
    provider, model = providers.resolve("screen")
    assert provider.name == "ollama" and model == "llama3.1"


def test_unknown_provider_lists_the_valid_ones(clean_env):
    clean_env.setenv("LLM_PROVIDER", "gpt5-turbo-ultra")
    with pytest.raises(LLMError, match="anthropic"):
        providers.resolve("screen")


def test_both_stages_ask_for_json_mode_and_leave_room_for_thinking():
    """Reasoning models spend output tokens before the answer starts. A ceiling
    sized to the visible answer gets eaten and you get truncated JSON."""
    jobs = make_jobs(1)
    s = StubProvider([scores_reply(jobs)])
    llm.screen(jobs, PROFILE, provider=s, model="m")
    assert s.calls[0]["json_mode"] is True
    assert s.calls[0]["max_tokens"] >= 4000

    d = StubProvider(['{"fit_summary":"x"}'])
    llm.draft(jobs, PROFILE, provider=d, model="m")
    assert d.calls[0]["json_mode"] is True
    assert d.calls[0]["max_tokens"] >= 8000


def test_providers_without_document_support_say_so():
    with pytest.raises(providers.UnsupportedDocument):
        providers.GroqProvider().complete_document("m", "prompt", b"%PDF", 100)
