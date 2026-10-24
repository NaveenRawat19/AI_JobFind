"""Parsers + prefilter, run against the fixtures in their native ATS shapes.

No network, no API key. This is the suite that catches the two bugs that cost
me an evening each: Lever's epoch-milliseconds timestamps, and a bare `sde`
regex that silently matches nothing.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt import mock
from jobhunt.fetch import parse_ashby, parse_greenhouse, parse_lever, strip_html
from jobhunt.mock import fetch_all_mock
from jobhunt.prefilter import prefilter

CONFIG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml")
                        .read_text(encoding="utf-8"))
FILTERS = CONFIG["filters"]


# ------------------------------------------------------------- strip_html ---

def test_strip_html_unescapes_twice():
    """Greenhouse ships HTML-entity-escaped HTML: unescape, strip, unescape."""
    raw = "&lt;p&gt;Go &amp;amp; Java&lt;/p&gt;"
    assert strip_html(raw) == "Go & Java"


def test_strip_html_turns_block_tags_into_newlines():
    out = strip_html("<p>One</p><p>Two</p><ul><li>a</li><li>b</li></ul>")
    assert "One" in out and "Two" in out and "a" in out and "b" in out
    assert "<" not in out


def test_strip_html_handles_none_and_empty():
    assert strip_html(None) == ""
    assert strip_html("") == ""


# ---------------------------------------------------------------- parsers ---

def test_greenhouse_maps_every_field():
    jobs = parse_greenhouse("acme-edge", "Acme Edge", mock.GREENHOUSE["acme-edge"])
    j = next(j for j in jobs if j.title.startswith("Software Engineer II"))
    assert j.job_id == "greenhouse:acme-edge:5501001"
    assert j.ats == "greenhouse"
    assert j.company == "Acme Edge"
    assert j.location == "Bangalore, India"
    assert j.url.startswith("https://boards.greenhouse.io/")
    assert "distributed services" in j.description


def test_lever_concatenates_description_lists_and_additional():
    """The requirements live in lists[], not descriptionPlain. Drop the
    concatenation and every Lever job looks unqualified."""
    jobs = parse_lever("quantstack", "QuantStack", mock.LEVER["quantstack"])
    j = next(j for j in jobs if j.title == "Backend Engineer (Go)")
    assert "market data pipeline" in j.description      # descriptionPlain
    assert "Requirements" in j.description              # lists[].text
    assert "2-5 years backend experience" in j.description  # lists[].content
    assert "No take-home" in j.description              # additionalPlain


def test_lever_createdAt_is_epoch_milliseconds():
    """1.7e12 is milliseconds. Reading it as seconds dates the post to 1970
    and the freshness filter eats the whole board without a word."""
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).date()
    jobs = parse_lever("quantstack", "QuantStack", mock.LEVER["quantstack"])
    j = next(j for j in jobs if j.title == "Backend Engineer (Go)")
    assert j.posted_at == two_days_ago.isoformat()


def test_ashby_skips_unlisted_drafts():
    jobs = parse_ashby("helioscale", "Helioscale", mock.ASHBY["helioscale"])
    assert all("unlisted" not in j.url for j in jobs)
    assert len(jobs) == 2   # 3 postings, one isListed: false


def test_ashby_reads_compensation_and_html_fallback():
    jobs = parse_ashby("helioscale", "Helioscale", mock.ASHBY["helioscale"])
    networking = next(j for j in jobs if j.title == "Software Engineer, Networking")
    assert networking.salary == "₹32L – ₹48L"
    ds = next(j for j in jobs if j.title == "Data Scientist, Growth")
    assert "Causal inference" in ds.description   # descriptionHtml fallback


def test_job_ids_are_globally_unique_and_namespaced():
    jobs = fetch_all_mock()
    ids = [j.job_id for j in jobs]
    assert len(ids) == len(set(ids))
    assert all(re.match(r"^(greenhouse|lever|ashby):[^:]+:.+$", i) for i in ids)


def test_parsers_take_decoded_json_not_a_response():
    """Parsers are pure: body in, list[Job] out. That is what makes --mock
    exercise the real code path instead of a second implementation."""
    assert parse_greenhouse("x", "X", {}) == []
    assert parse_lever("x", "X", []) == []
    assert parse_ashby("x", "X", {}) == []


# -------------------------------------------------------------- prefilter ---

@pytest.mark.parametrize("title", [
    "Software Engineer II, Distributed Systems",
    "Software Development Engineer, Core Infra",
    "Backend Engineer (Go)",
    "Site Reliability Engineer",
    "SDE II",
])
def test_include_titles_match_real_titles(title):
    inc = FILTERS["include_titles"]
    assert any(re.search(p, title, re.I) for p in inc), title


def test_bare_sde_regex_does_not_match_the_spelled_out_title():
    """The bug: `sde` looks like it covers "Software Development Engineer".
    It does not — they share no substring. \\bsde\\b plus the spelled-out
    variant is why both titles survive the filter."""
    assert not re.search(r"\bsde\b", "Software Development Engineer", re.I)
    assert re.search(r"\bsde\b", "SDE II", re.I)
    inc = FILTERS["include_titles"]
    assert any(re.search(p, "Software Development Engineer, Core Infra", re.I) for p in inc)


@pytest.mark.parametrize("title", [
    "Staff Software Engineer, Storage",       # too senior
    "Engineering Manager, Platform",          # management track
    "Enterprise Account Executive",           # wrong function
    "Frontend Engineer, Design Systems",      # wrong discipline
    "Data Scientist, Growth",                 # wrong discipline
])
def test_junk_titles_are_rejected(title):
    inc, exc = FILTERS["include_titles"], FILTERS["exclude_titles"]
    included = any(re.search(p, title, re.I) for p in inc)
    excluded = any(re.search(p, title, re.I) for p in exc)
    assert excluded or not included, f"{title!r} would have survived"


def test_full_mock_funnel_keeps_only_the_five_real_matches():
    kept = prefilter(fetch_all_mock(), FILTERS)
    titles = sorted(j.title for j in kept)
    assert titles == [
        "Backend Engineer (Go)",
        "Site Reliability Engineer",
        "Software Development Engineer, Core Infra",
        "Software Engineer II, Distributed Systems",
        "Software Engineer, Networking",
    ]


def test_stale_posting_is_dropped_by_freshness_gate():
    kept = prefilter(fetch_all_mock(), FILTERS)
    assert not any("Senior Software Engineer, Platform" == j.title for j in kept)


def test_wrong_city_dropped_but_remote_kept():
    kept = prefilter(fetch_all_mock(), FILTERS)
    assert not any("San Francisco" in (j.location or "") for j in kept)
    assert any("Remote" in (j.location or "") for j in kept)


def test_allow_remote_is_what_lets_an_out_of_region_remote_role_through():
    """"Remote (India)" already matches the `india` location, so it is the
    wrong fixture for this. Use a remote role that names no allowed city."""
    from jobhunt.fetch import Job
    remote = Job(job_id="lever:x:1", ats="lever", company="X",
                 title="Backend Engineer", location="Remote - Global",
                 url="https://example.com", description="Go")

    kept_on = prefilter([remote], dict(FILTERS, allow_remote=True))
    kept_off = prefilter([remote], dict(FILTERS, allow_remote=False))

    assert len(kept_on) == 1
    assert kept_off == []


def test_empty_filters_keep_everything():
    jobs = fetch_all_mock()
    assert len(prefilter(jobs, {})) == len(jobs)


# ---------------------------------------------------------------- workday ---

def test_workday_url_parsing():
    from jobhunt.fetch import parse_workday_url
    assert parse_workday_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite") == (
        "nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert parse_workday_url("https://wf.wd1.myworkdayjobs.com/en-US/WellsFargoJobs/jobs") == (
        "wf", "wd1", "WellsFargoJobs")
    with pytest.raises(ValueError):
        parse_workday_url("https://example.com/careers")


def test_workday_posted_text_to_date():
    from datetime import date
    from jobhunt.fetch import parse_workday_posted
    today = date(2026, 10, 24)
    assert parse_workday_posted("Posted Today", today) == "2026-10-24"
    assert parse_workday_posted("Posted Yesterday", today) == "2026-10-23"
    assert parse_workday_posted("Posted 18 Days Ago", today) == "2026-10-06"
    assert parse_workday_posted("Posted 30+ Days Ago", today) == "2026-09-24"
    assert parse_workday_posted(None, today) is None


def test_workday_fetch_skips_detail_for_filtered_rows():
    from jobhunt.fetch import fetch_workday
    from jobhunt.prefilter import reject_reason

    rows = [
        {"title": "Backend Engineer", "externalPath": "/job/Pune/Backend-Engineer_JR1",
         "locationsText": "Pune, India", "postedOn": "Posted Today", "bulletFields": ["JR1"]},
        {"title": "Senior Backend Engineer", "externalPath": "/job/Pune/Senior_JR2",
         "locationsText": "Pune, India", "postedOn": "Posted Today", "bulletFields": ["JR2"]},
        {"title": "Software Engineer", "externalPath": "/job/Multi/SWE_JR3",
         "locationsText": "2 Locations", "postedOn": "Posted Yesterday", "bulletFields": ["JR3"]},
    ]
    detail = {"jobPostingInfo": {"jobDescription": "<p>Python &amp; FastAPI</p>",
                                 "location": "Hyderabad, India", "startDate": "2026-10-23",
                                 "externalUrl": "https://x.wd1.myworkdayjobs.com/Site/job/x"}}

    class Resp:
        def __init__(self, body): self.status_code, self._b = 200, body
        def json(self): return self._b

    class Sess:
        gets = []
        def post(self, url, **kw):
            return Resp({"jobPostings": rows if kw["json"]["offset"] == 0 else []})
        def get(self, url, **kw):
            self.gets.append(url)
            return Resp(detail)

    sess = Sess()
    jobs = fetch_workday(
        {"url": "https://x.wd1.myworkdayjobs.com/Site", "name": "X"}, session=sess,
        keep=lambda j: reject_reason(j, FILTERS, location_unknown_ok=True) is None,
        sleep=0, search_terms=["engineer"])

    assert len(sess.gets) == 2                       # "Senior" row never cost a request
    assert {j.job_id for j in jobs} == {"workday:x:JR1", "workday:x:JR3"}
    assert all("Python & FastAPI" in j.description for j in jobs)
    assert jobs[0].location == "Hyderabad, India"
