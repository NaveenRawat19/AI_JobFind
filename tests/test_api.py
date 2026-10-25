"""API tests: no network, no API key. The LLM and the job boards are stubbed."""
from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt import llm, pipeline
from jobhunt.api import app as app_module
from jobhunt.api.app import create_app
from jobhunt.api.db import Database
from jobhunt.fetch import Job
from jobhunt.providers import LLMError

SECRET = "test-secret-" + "x" * 32
PASSWORD = "correct horse battery"


def _job(i: int, title: str, company: str = "Acme", location: str = "Bengaluru, India") -> Job:
    return Job(job_id=f"greenhouse:{company.lower()}:{i}", ats="greenhouse", company=company,
               title=title, location=location, url=f"https://example.com/{i}",
               description=f"Python and FastAPI role {i}", posted_at="2026-10-24")


BOARD = [
    _job(1, "Backend Engineer"),
    _job(2, "Software Engineer", "Globex"),
    _job(3, "Staff Engineer"),                         # dropped by the title filter
    _job(4, "Software Engineer", location="Austin, TX"),  # dropped by the location filter
]
SCORES = {"Backend Engineer": 9.0, "Software Engineer": 5.0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"ats": "greenhouse", "slug": "acme", "name": "Acme"},
        {"ats": "greenhouse", "slug": "globex", "name": "Globex"},
    ]}))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "filters": {"include_titles": ["engineer"], "exclude_titles": [r"\bstaff\b"],
                    "locations": ["india"], "allow_remote": True, "max_age_days": None},
        "score_threshold": 7.0, "max_per_digest": 5,
        "companies_file": "companies.yaml",
    }))
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("MIN_RUN_INTERVAL_MIN", "0")
    for name in ("SESSION_IDLE_MINUTES", "SESSION_MAX_HOURS", "MAX_FAILED_LOGINS",
                 "LOCKOUT_MINUTES", "ALLOW_REGISTRATION", "MAX_CONCURRENT_RUNS"):
        monkeypatch.delenv(name, raising=False)

    def fake_fetch(companies, keep=None, workday=None, **_):
        slug = companies[0]["slug"]
        return [replace(j) for j in BOARD
                if j.company.lower() == slug and (keep is None or keep(j))]

    def fake_screen(jobs, profile, **_):
        for j in jobs:
            j.score, j.reason = SCORES[j.title], "stub"
        return jobs

    def fake_draft(jobs, profile, **_):
        for j in jobs:
            j.draft = {"fit_summary": "fits", "tailored_bullets": ["b"], "gaps": [],
                       "cover_note": "hello", "questions_to_ask": ["q"]}
        return jobs

    monkeypatch.setattr(pipeline, "fetch_all", fake_fetch)
    monkeypatch.setattr(pipeline, "resolve", lambda stage, check=True: (object(), "stub-model"))
    monkeypatch.setattr(app_module, "resolve", lambda stage, check=True: (object(), "stub-model"))
    monkeypatch.setattr(llm, "screen", fake_screen)
    monkeypatch.setattr(llm, "draft", fake_draft)
    monkeypatch.setattr(llm, "build_profile", lambda **kw: {
        "name": "Test User", "years_experience": "3.2", "core_skills": ["Python"],
        "seniority": "mid", "surprise_field": "dropped"})

    with TestClient(create_app(tmp_path / "t.db", tmp_path / "config.yaml")) as c:
        yield c


def signup(client, email="a@example.com", password=PASSWORD) -> dict:
    assert client.post("/auth/register", json={"email": email, "password": password}).status_code == 201
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def h(client) -> dict:
    """Auth headers for the default user."""
    return signup(client)


def upload(client, h, name="cv.pdf", data=b"%PDF-1.4 fake"):
    return client.post("/resume", headers=h, files={"file": (name, data)})


def sql(client, query, *args):
    with client.app.state.db.connect() as c:
        c.execute(query, args)


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def token_with(**claims) -> str:
    payload = {"sub": "1", "sid": "x", "exp": int(time.time()) + 600, **claims}
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, SECRET, algorithm="HS256")


# --------------------------------------------------------------- register ---

def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_register_creates_account_and_never_returns_the_hash(client):
    r = client.post("/auth/register", json={"email": "New@Example.com", "password": PASSWORD})
    assert r.status_code == 201
    assert r.json()["email"] == "new@example.com"            # normalised
    assert "password" not in r.text and "hash" not in r.text


def test_password_is_stored_hashed(client):
    signup(client)
    user = client.app.state.db.get_user_by_email("a@example.com")
    assert user["password_hash"].startswith("scrypt$") and PASSWORD not in user["password_hash"]


def test_duplicate_email_is_409_case_insensitive(client):
    signup(client, "a@example.com")
    r = client.post("/auth/register", json={"email": "A@EXAMPLE.COM", "password": PASSWORD})
    assert r.status_code == 409


@pytest.mark.parametrize("body", [
    {"email": "not-an-email", "password": PASSWORD},
    {"email": "a@example.com", "password": "short"},
    {"email": "a@example.com", "password": "x" * 129},
    {"email": "a@example.com"},
    {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
])
def test_register_validation(client, body):
    assert client.post("/auth/register", json=body).status_code == 422


def test_registration_can_be_closed(client, monkeypatch):
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    r = client.post("/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 403


# ------------------------------------------------------------------ login ---

def test_login_returns_a_bearer_token_with_the_idle_timeout(client):
    signup(client)
    r = client.post("/auth/login", data={"username": "A@Example.com", "password": PASSWORD})
    body = r.json()
    assert r.status_code == 200 and body["token_type"] == "bearer"
    assert body["idle_timeout_seconds"] == 1800               # 30 minutes
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"])
    assert claims["sub"] == "1" and claims["sid"] and claims["exp"] > time.time()


def test_wrong_password_and_unknown_email_look_identical(client):
    signup(client)
    wrong = client.post("/auth/login", data={"username": "a@example.com", "password": "nope-nope-nope"})
    unknown = client.post("/auth/login", data={"username": "ghost@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.headers["www-authenticate"] == "Bearer"


def test_oversized_password_is_rejected_without_hashing(client):
    signup(client)
    r = client.post("/auth/login", data={"username": "a@example.com", "password": "x" * 5000})
    assert r.status_code == 401


def test_account_locks_after_repeated_failures_then_recovers(client, monkeypatch):
    monkeypatch.setenv("MAX_FAILED_LOGINS", "3")
    signup(client)
    bad = {"username": "a@example.com", "password": "wrong-wrong"}
    assert [client.post("/auth/login", data=bad).status_code for _ in range(3)] == [401, 401, 401]

    r = client.post("/auth/login", data=bad)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # the right password does not get through while locked
    good = {"username": "a@example.com", "password": PASSWORD}
    assert client.post("/auth/login", data=good).status_code == 429

    sql(client, "UPDATE users SET locked_until=?", ago(minutes=1))     # lockout elapses
    assert client.post("/auth/login", data=good).status_code == 200
    user = client.app.state.db.get_user_by_email("a@example.com")
    assert user["failed_attempts"] == 0 and user["locked_until"] is None


def test_successful_login_resets_the_failure_counter(client):
    signup(client)
    client.post("/auth/login", data={"username": "a@example.com", "password": "wrong-wrong"})
    client.post("/auth/login", data={"username": "a@example.com", "password": PASSWORD})
    assert client.app.state.db.get_user_by_email("a@example.com")["failed_attempts"] == 0


# ----------------------------------------------------------------- tokens ---

@pytest.mark.parametrize("path", ["/jobs", "/profile", "/stats", "/runs/latest", "/auth/me"])
def test_protected_endpoints_need_a_token(client, path):
    r = client.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_post_endpoints_need_a_token(client):
    assert client.post("/search").status_code == 401
    assert client.post("/resume", files={"file": ("cv.txt", b"x")}).status_code == 401
    assert client.post("/jobs/x/applied").status_code == 401


def test_bad_tokens_are_rejected(client, h):
    forged = jwt.encode({"sub": "1", "sid": "x", "exp": int(time.time()) + 600},
                        "some-other-secret-" + "y" * 32, algorithm="HS256")
    none_alg = jwt.encode({"sub": "1", "sid": "x", "exp": int(time.time()) + 600}, None, algorithm="none")
    tampered = h["Authorization"].split(" ")[1][:-3] + "abc"
    for token in ["garbage", forged, none_alg, tampered,
                  token_with(exp=int(time.time()) - 10),        # expired
                  token_with(sid=None),                          # required claim missing
                  token_with(sub="not-a-number")]:
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token


def test_a_valid_signature_for_an_unknown_session_is_rejected(client, h):
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token_with(sid='never-issued')}"})
    assert r.status_code == 401


def test_me_returns_the_user(client, h):
    body = client.get("/auth/me", headers=h).json()
    assert body["email"] == "a@example.com" and body["idle_timeout_seconds"] == 1800
    assert "password_hash" not in body


def test_has_profile_flag_tells_clients_to_onboard(client):
    h = signup(client)
    login = lambda: client.post("/auth/login", data={"username": "a@example.com", "password": PASSWORD}).json()
    assert login()["has_profile"] is False
    assert client.get("/auth/me", headers=h).json()["has_profile"] is False
    upload(client, h)
    assert login()["has_profile"] is True
    assert client.get("/auth/me", headers=h).json()["has_profile"] is True


# ------------------------------------------------------- inactivity timeout ---

def test_session_dies_after_30_minutes_idle(client, h):
    assert client.get("/auth/me", headers=h).status_code == 200
    sql(client, "UPDATE sessions SET last_activity=?", ago(minutes=31))
    r = client.get("/auth/me", headers=h)
    assert r.status_code == 401 and r.json()["detail"] == "session expired"
    # and it stays dead: the JWT itself has hours left, but the session is gone
    assert client.get("/jobs", headers=h).status_code == 401


def test_session_survives_just_inside_the_window(client, h):
    sql(client, "UPDATE sessions SET last_activity=?", ago(minutes=29))
    assert client.get("/auth/me", headers=h).status_code == 200


def test_activity_slides_the_idle_window(client, h):
    sql(client, "UPDATE sessions SET last_activity=?", ago(minutes=25))
    assert client.get("/auth/me", headers=h).status_code == 200      # touches the session
    with client.app.state.db.connect() as c:
        last = c.execute("SELECT last_activity FROM sessions").fetchone()[0]
    assert datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(minutes=1)
    # 25 minutes after the touch it would be 50 since login: still alive
    sql(client, "UPDATE sessions SET last_activity=?", ago(minutes=25))
    assert client.get("/auth/me", headers=h).status_code == 200


def test_failed_requests_do_not_extend_the_session(client, h):
    sql(client, "UPDATE sessions SET last_activity=?", ago(minutes=31))
    client.get("/auth/me", headers=h)
    with client.app.state.db.connect() as c:
        last = c.execute("SELECT last_activity FROM sessions").fetchone()[0]
    assert datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(minutes=30)


def test_idle_timeout_is_configurable(client, h, monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "1")
    sql(client, "UPDATE sessions SET last_activity=?", ago(minutes=2))
    assert client.get("/auth/me", headers=h).status_code == 401


def test_idle_timeout_is_reported_at_login(client, monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "10")
    signup(client)
    r = client.post("/auth/login", data={"username": "a@example.com", "password": PASSWORD})
    assert r.json()["idle_timeout_seconds"] == 600


def test_absolute_session_limit_applies_even_when_active(client, monkeypatch):
    monkeypatch.setenv("SESSION_MAX_HOURS", "1")
    h = signup(client)
    claims = jwt.decode(h["Authorization"].split(" ")[1], SECRET, algorithms=["HS256"])
    assert 3500 < claims["exp"] - time.time() <= 3600


def test_logout_revokes_the_session_immediately(client, h):
    assert client.post("/auth/logout", headers=h).status_code == 204
    assert client.get("/auth/me", headers=h).status_code == 401


def test_logout_only_ends_that_session(client):
    h1 = signup(client)
    r = client.post("/auth/login", data={"username": "a@example.com", "password": PASSWORD})
    h2 = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/auth/logout", headers=h1)
    assert client.get("/auth/me", headers=h1).status_code == 401
    assert client.get("/auth/me", headers=h2).status_code == 200


# ---------------------------------------------------------- misconfiguration ---

@pytest.mark.parametrize("secret", [None, "too-short"])
def test_missing_or_weak_jwt_secret_disables_auth(client, monkeypatch, secret):
    if secret is None:
        monkeypatch.delenv("JWT_SECRET")
    else:
        monkeypatch.setenv("JWT_SECRET", secret)
    assert client.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD}).status_code == 503
    assert client.post("/auth/login", data={"username": "b@example.com", "password": PASSWORD}).status_code == 503
    assert client.get("/jobs", headers={"Authorization": "Bearer x"}).status_code == 503


def test_pre_auth_database_is_moved_aside_not_destroyed(tmp_path):
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('a', 'kept')")
    conn.commit()
    conn.close()

    Database(old).init()
    backup = sqlite3.connect(tmp_path / "old.pre-auth.bak")
    assert backup.execute("SELECT title FROM jobs").fetchone()[0] == "kept"
    backup.close()
    cols = [r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(jobs)")]
    assert "user_id" in cols


# ----------------------------------------------------------------- resume ---

def test_resume_upload_stores_normalised_profile(client, h):
    r = upload(client, h)
    assert r.status_code == 200
    body = r.json()
    assert body["source_file"] == "cv.pdf"
    assert body["profile"]["years_experience"] == 3.2          # "3.2" coerced to a number
    assert "surprise_field" not in body["profile"]
    assert client.get("/profile", headers=h).json()["profile"]["name"] == "Test User"


def test_resume_upload_validation(client, h):
    assert upload(client, h, "cv.docx").status_code == 415
    assert upload(client, h, "cv.pdf", b"not a pdf").status_code == 415
    assert upload(client, h, "cv.txt", b"   ").status_code == 422
    assert upload(client, h, "cv.txt", b"x" * (5 * 1024 * 1024 + 1)).status_code == 413


def test_resume_text_file_is_accepted(client, h):
    assert upload(client, h, "cv.md", b"# Test User\nPython engineer").status_code == 200


def test_resume_llm_failure_is_502(client, h, monkeypatch):
    def boom(**kw):
        raise LLMError("quota")
    monkeypatch.setattr(llm, "build_profile", boom)
    r = upload(client, h)
    assert r.status_code == 502 and "quota" in r.json()["detail"]


def test_profile_404_before_upload_and_partial_update_after(client, h):
    assert client.get("/profile", headers=h).status_code == 404
    assert client.put("/profile", headers=h, json={"seniority": "mid"}).status_code == 404
    upload(client, h)
    r = client.put("/profile", headers=h, json={"years_experience": 4})
    assert r.json()["profile"]["years_experience"] == 4
    assert r.json()["profile"]["name"] == "Test User"          # untouched
    assert client.put("/profile", headers=h, json={"bogus": 1}).status_code == 422


# ----------------------------------------------------------------- search ---

def test_search_requires_a_profile(client, h):
    assert client.post("/search", headers=h).status_code == 409


def test_full_run_filters_scores_and_drafts(client, h):
    upload(client, h)
    r = client.post("/search", headers=h)
    assert r.status_code == 202
    run = client.get(f"/runs/{r.json()['run_id']}", headers=h).json()
    assert run["status"] == "succeeded", run
    assert run["counts"] == {"scanned": 2, "passed_filters": 2, "new": 2,
                             "screened": 2, "matches": 1}

    # default min_score is the config threshold (7), so only the 9.0 shows
    jobs = client.get("/jobs", headers=h).json()
    assert [j["title"] for j in jobs["items"]] == ["Backend Engineer"]
    assert jobs["items"][0]["has_draft"] is True
    assert client.get("/jobs?min_score=0", headers=h).json()["total"] == 2

    detail = client.get("/jobs/greenhouse:acme:1", headers=h).json()
    assert detail["draft"]["cover_note"] == "hello"
    assert "FastAPI" in detail["description"]
    # a below-threshold job is stored and scored but not drafted
    low = client.get("/jobs/greenhouse:globex:2", headers=h).json()
    assert low["score"] == 5.0 and low["draft"] is None


def test_second_run_skips_jobs_already_seen(client, h):
    upload(client, h)
    client.post("/search", headers=h)
    r = client.post("/search", headers=h)
    run = client.get(f"/runs/{r.json()['run_id']}", headers=h).json()
    assert run["status"] == "failed" and "no postings fetched" in run["error"]


def test_search_can_be_limited_to_named_companies(client, h):
    upload(client, h)
    run_id = client.post("/search", headers=h, json={"companies": ["acme"]}).json()["run_id"]
    run = client.get(f"/runs/{run_id}", headers=h).json()
    assert run["counts"]["scanned"] == 1
    assert client.post("/search", headers=h, json={"companies": ["nope"]}).status_code == 422


def test_search_without_draft_skips_the_expensive_stage(client, h):
    upload(client, h)
    client.post("/search", headers=h, json={"draft": False})
    assert client.get("/jobs/greenhouse:acme:1", headers=h).json()["draft"] is None


def test_rate_limit_returns_429_with_retry_after(client, h, monkeypatch):
    monkeypatch.setenv("MIN_RUN_INTERVAL_MIN", "30")
    upload(client, h)
    assert client.post("/search", headers=h).status_code == 202
    r = client.post("/search", headers=h)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_search_while_a_run_is_in_flight_is_409(client, h):
    upload(client, h)
    in_flight = client.app.state.db.start_run(1, {}, 0, 10)   # started, never finished
    r = client.post("/search", headers=h)
    assert r.status_code == 409 and in_flight["run_id"] in r.json()["detail"]


def test_db_blocks_second_running_run_per_user(tmp_path):
    db = Database(tmp_path / "x.db")
    db.init()
    uid = db.create_user("a@example.com", "h")["id"]
    first = db.start_run(uid, {}, 0, 10)
    with pytest.raises(Exception) as e:
        db.start_run(uid, {}, 0, 10)
    assert e.value.kind == "running" and e.value.run_id == first["run_id"]


def test_stale_running_run_is_failed_on_restart(tmp_path):
    db = Database(tmp_path / "x.db")
    db.init()
    uid = db.create_user("a@example.com", "h")["id"]
    rid = db.start_run(uid, {}, 0, 10)["run_id"]
    db.init()
    assert db.get_run(uid, rid)["status"] == "failed"
    assert db.start_run(uid, {}, 0, 10)["status"] == "running"      # no longer blocked


def test_server_wide_run_cap_returns_503(client, h, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "1")
    hb = signup(client, "b@example.com")
    upload(client, h)
    upload(client, hb)
    client.app.state.db.start_run(1, {}, 0, 10)               # user A's run is in flight
    r = client.post("/search", headers=hb)
    assert r.status_code == 503 and int(r.headers["Retry-After"]) > 0


def test_missing_llm_key_fails_fast_before_fetching(client, h, monkeypatch):
    def no_key(stage, check=True):
        raise LLMError("GEMINI_API_KEY is not set")
    fetched = []
    monkeypatch.setattr(pipeline, "resolve", no_key)
    monkeypatch.setattr(pipeline, "fetch_all", lambda *a, **k: fetched.append(1) or [])
    upload(client, h)
    run_id = client.post("/search", headers=h).json()["run_id"]
    run = client.get(f"/runs/{run_id}", headers=h).json()
    assert run["status"] == "failed" and "GEMINI_API_KEY" in run["error"]
    assert fetched == []


def test_unscored_jobs_are_not_recorded_so_they_retry(client, h, monkeypatch):
    def half(jobs, profile, **_):
        jobs[0].score, jobs[0].reason = 9.0, "ok"
        return jobs
    monkeypatch.setattr(llm, "screen", half)
    upload(client, h)
    run_id = client.post("/search", headers=h).json()["run_id"]
    run = client.get(f"/runs/{run_id}", headers=h).json()
    assert run["counts"]["screened"] == 1 and run["warnings"]
    assert client.get("/jobs?min_score=0", headers=h).json()["total"] == 1


# ------------------------------------------------------------------- jobs ---

def test_job_filters_pagination_and_applied(client, h):
    upload(client, h)
    client.post("/search", headers=h)
    j = client.get("/jobs?min_score=0&limit=1", headers=h).json()
    assert j["total"] == 2 and len(j["items"]) == 1 and j["items"][0]["score"] == 9.0
    assert client.get("/jobs?min_score=0&company=globex", headers=h).json()["total"] == 1
    assert client.get("/jobs?min_score=0&q=backend", headers=h).json()["total"] == 1
    assert client.get("/jobs?min_score=0&location=india", headers=h).json()["total"] == 2
    assert client.get("/jobs?status=bogus", headers=h).status_code == 422

    r = client.post("/jobs/greenhouse:acme:1/applied", headers=h)
    assert r.json()["status"] == "applied" and r.json()["applied_on"]
    assert client.get("/jobs?min_score=0&status=applied", headers=h).json()["total"] == 1
    assert client.post("/jobs/nope/applied", headers=h).status_code == 404
    assert client.get("/jobs/nope", headers=h).status_code == 404
    assert client.get("/stats", headers=h).json() == {"tracked": 2, "applied": 1, "drafted": 1}


def test_latest_run_endpoint(client, h):
    assert client.get("/runs/latest", headers=h).status_code == 404
    upload(client, h)
    rid = client.post("/search", headers=h).json()["run_id"]
    assert client.get("/runs/latest", headers=h).json()["run_id"] == rid


# --------------------------------------------------------- user isolation ---

def test_users_cannot_see_each_others_data(client, h):
    hb = signup(client, "b@example.com")
    upload(client, h)
    rid = client.post("/search", headers=h).json()["run_id"]
    client.post("/jobs/greenhouse:acme:1/applied", headers=h)

    assert client.get("/profile", headers=hb).status_code == 404
    assert client.get("/jobs?min_score=0", headers=hb).json()["total"] == 0
    assert client.get("/jobs/greenhouse:acme:1", headers=hb).status_code == 404
    assert client.post("/jobs/greenhouse:acme:1/applied", headers=hb).status_code == 404
    assert client.get(f"/runs/{rid}", headers=hb).status_code == 404
    assert client.get("/runs/latest", headers=hb).status_code == 404
    assert client.get("/stats", headers=hb).json()["tracked"] == 0


def test_each_user_gets_their_own_copy_of_the_same_job(client, h):
    hb = signup(client, "b@example.com")
    for headers in (h, hb):
        upload(client, headers)
        client.post("/search", headers=headers)
    assert client.get("/jobs?min_score=0", headers=h).json()["total"] == 2
    assert client.get("/jobs?min_score=0", headers=hb).json()["total"] == 2
    client.post("/jobs/greenhouse:acme:1/applied", headers=h)
    assert client.get("/jobs/greenhouse:acme:1", headers=hb).json()["status"] == "new"


def test_run_rate_limit_is_per_user(client, h, monkeypatch):
    monkeypatch.setenv("MIN_RUN_INTERVAL_MIN", "30")
    hb = signup(client, "b@example.com")
    upload(client, h)
    upload(client, hb)
    assert client.post("/search", headers=h).status_code == 202
    assert client.post("/search", headers=h).status_code == 429
    assert client.post("/search", headers=hb).status_code == 202


def test_profiles_are_independent(client, h):
    hb = signup(client, "b@example.com")
    upload(client, h)
    upload(client, hb)
    client.put("/profile", headers=h, json={"years_experience": 9})
    assert client.get("/profile", headers=hb).json()["profile"]["years_experience"] == 3.2


# --------------------------------------------------------------- insights ---

def seed(client, user_id, rows):
    """rows: (title, description, score). Stored directly, no pipeline run."""
    jobs = []
    for i, (title, desc, score) in enumerate(rows):
        j = _job(100 + i, title)
        j.job_id = f"greenhouse:seed:{user_id}:{i}"
        j.description, j.score = desc, score
        jobs.append(j)
    client.app.state.db.save_jobs(user_id, jobs, "seed")


AI_JD = "You will build RAG systems with LLMs and vector databases on AWS using Python."
BE_JD = "Build REST APIs in Java and Spring Boot on AWS with PostgreSQL."


def test_insights_need_a_token(client):
    assert client.get("/insights/skills").status_code == 401


def test_insights_empty_state_says_what_to_do(client, h):
    body = client.get("/insights/skills", headers=h).json()
    assert body["sample_size"] == 0 and body["skills"] == [] and "POST /search" in body["note"]


def test_insights_counts_and_percentages_come_from_stored_postings(client, h):
    seed(client, 1, [("AI Engineer", AI_JD, 9), ("ML Engineer", AI_JD, 4),
                     ("Backend Engineer", BE_JD, 8), ("Backend Engineer", BE_JD, 2)])
    body = client.get("/insights/skills", headers=h).json()
    by = {s["skill"]: s for s in body["skills"]}
    assert body["sample_size"] == 4 and body["with_description"] == 4
    assert by["AWS"]["jobs"] == 4 and by["AWS"]["percent"] == 100.0
    assert by["RAG"]["jobs"] == 2 and by["RAG"]["percent"] == 50.0
    assert by["Java"]["jobs"] == 2 and "JavaScript" not in by
    assert [s["jobs"] for s in body["skills"]] == sorted((s["jobs"] for s in body["skills"]), reverse=True)


def test_insights_default_includes_low_scored_postings_but_min_score_filters(client, h):
    seed(client, 1, [("AI Engineer", AI_JD, 9), ("AI Engineer", AI_JD, 3)])
    assert client.get("/insights/skills", headers=h).json()["sample_size"] == 2
    assert client.get("/insights/skills?min_score=7", headers=h).json()["sample_size"] == 1


def test_insights_role_filter_matches_how_the_user_would_ask(client, h):
    seed(client, 1, [("AI Engineer", AI_JD, 9), ("ML Engineer", AI_JD, 9), ("Backend Engineer", BE_JD, 9)])
    body = client.get("/insights/skills?role=ai-ml", headers=h).json()
    names = {s["skill"] for s in body["skills"]}
    assert body["sample_size"] == 2 and "RAG" in names and "Spring Boot" not in names
    # the role breakdown is reported for the whole filtered set, so a UI can offer the choices
    assert {r["role"]: r["jobs"] for r in body["roles"]} == {"ai-ml": 2, "backend": 1}


def test_insights_category_and_top(client, h):
    seed(client, 1, [("AI Engineer", AI_JD, 9)])
    body = client.get("/insights/skills?category=ai&top=2", headers=h).json()
    assert len(body["skills"]) == 2 and {s["category"] for s in body["skills"]} == {"ai"}


def test_insights_rejects_unknown_role_and_category(client, h):
    assert client.get("/insights/skills?role=wizard", headers=h).status_code == 422
    assert client.get("/insights/skills?category=wizard", headers=h).status_code == 422
    assert client.get("/insights/skills?top=0", headers=h).status_code == 422


def test_insights_flags_gaps_against_the_profile(client, h):
    seed(client, 1, [("AI Engineer", AI_JD, 9)] * 3)
    none = client.get("/insights/skills", headers=h).json()
    assert all(s["in_profile"] is None for s in none["skills"]) and none["gaps"] == []

    upload(client, h)                                           # core_skills: ["Python"]
    body = client.get("/insights/skills", headers=h).json()
    by = {s["skill"]: s for s in body["skills"]}
    assert by["Python"]["in_profile"] is True and by["RAG"]["in_profile"] is False
    assert "RAG" in body["gaps"] and "Python" not in body["gaps"]


def test_insights_small_sample_is_called_out(client, h):
    seed(client, 1, [("AI Engineer", AI_JD, 9)])
    assert "small sample" in client.get("/insights/skills", headers=h).json()["note"]


def test_insights_warn_when_descriptions_are_missing(client, h):
    seed(client, 1, [("Software Engineer", "", 9) for _ in range(25)] + [("AI Engineer", AI_JD, 9)] * 2)
    body = client.get("/insights/skills", headers=h).json()
    assert body["with_description"] == 2 and "understated" in body["note"]


def test_insights_only_see_the_callers_postings(client, h):
    hb = signup(client, "b@example.com")
    seed(client, 1, [("AI Engineer", AI_JD, 9)])
    assert client.get("/insights/skills", headers=hb).json()["sample_size"] == 0
    assert client.get("/insights/skills", headers=h).json()["sample_size"] == 1


def test_insights_truncate_at_the_cap(client, h, monkeypatch):
    monkeypatch.setattr(app_module, "INSIGHTS_MAX_JOBS", 3)
    seed(client, 1, [("AI Engineer", AI_JD, 9)] * 5)
    body = client.get("/insights/skills", headers=h).json()
    assert body["sample_size"] == 3 and body["truncated"] is True


# ------------------------------------------------------- stored resume file ---

PDF = b"%PDF-1.4 fake resume bytes \x00\xff"


def test_upload_keeps_the_file_and_download_returns_identical_bytes(client, h):
    body = upload(client, h, "Naveen CV.pdf", PDF).json()
    info = body["resume"]
    assert info["filename"] == "Naveen CV.pdf" and info["size"] == len(PDF)
    assert info["media_type"] == "application/pdf" and len(info["sha256"]) == 64
    assert "data" not in info                                    # metadata only in JSON

    r = client.get("/resume", headers=h)
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert 'filename="Naveen CV.pdf"' in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in r.headers["cache-control"]
    assert client.get("/profile", headers=h).json()["resume"]["size"] == len(PDF)


def test_text_resumes_are_served_as_text_not_html(client, h):
    upload(client, h, "cv.md", b"# Test\n<script>alert(1)</script>")
    r = client.get("/resume", headers=h)
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-disposition"].startswith("attachment;")


def test_download_needs_a_token_and_a_stored_resume(client, h):
    for method, path in [("get", "/resume"), ("get", "/resumes"), ("get", "/resumes/1"),
                         ("delete", "/resumes/1"), ("post", "/resume/reextract")]:
        assert getattr(client, method)(path).status_code == 401, path
    assert client.get("/resume", headers=h).status_code == 404
    assert client.get("/resumes", headers=h).json() == []
    assert client.get("/resumes/1", headers=h).status_code == 404
    assert client.delete("/resumes/1", headers=h).status_code == 404
    assert client.post("/resume/reextract", headers=h).status_code == 404


def test_every_upload_is_kept_and_listed_newest_first(client, h):
    upload(client, h, "old.pdf", PDF)
    upload(client, h, "new.txt", b"Python engineer")
    upload(client, h, "newest.md", b"# Go engineer")
    items = client.get("/resumes", headers=h).json()
    assert [i["filename"] for i in items] == ["newest.md", "new.txt", "old.pdf"]
    assert [i["is_current"] for i in items] == [True, False, False]
    assert all("data" not in i and len(i["sha256"]) == 64 for i in items)
    assert client.get("/resume", headers=h).content == b"# Go engineer"        # newest
    old = items[2]
    r = client.get(f"/resumes/{old['id']}", headers=h)
    assert r.content == PDF and 'filename="old.pdf"' in r.headers["content-disposition"]
    assert client.get("/profile", headers=h).json()["resume"]["id"] == items[0]["id"]


def test_uploading_identical_bytes_again_does_not_add_a_duplicate(client, h):
    upload(client, h, "a.txt", b"Python engineer")
    upload(client, h, "b.txt", b"Go engineer")
    upload(client, h, "a-again.txt", b"Python engineer")
    items = client.get("/resumes", headers=h).json()
    assert [i["filename"] for i in items] == ["a-again.txt", "b.txt"]          # moved to the top
    assert client.get("/resume", headers=h).content == b"Python engineer"


def test_failed_extraction_keeps_the_previous_resume(client, h, monkeypatch):
    upload(client, h, "good.pdf", PDF)

    def boom(**kw):
        raise LLMError("quota")
    monkeypatch.setattr(llm, "build_profile", boom)
    assert upload(client, h, "bad.pdf", b"%PDF-1.4 other").status_code == 502
    assert [i["filename"] for i in client.get("/resumes", headers=h).json()] == ["good.pdf"]
    assert client.get("/profile", headers=h).json()["source_file"] == "good.pdf"


def test_rejected_uploads_store_nothing(client, h):
    upload(client, h, "cv.docx")
    upload(client, h, "cv.pdf", b"not a pdf")
    upload(client, h, "cv.txt", b"  ")
    assert client.get("/resumes", headers=h).json() == []


def test_per_user_limit_refuses_new_files_without_deleting_old_ones(client, h, monkeypatch):
    monkeypatch.setenv("MAX_RESUMES_PER_USER", "2")
    upload(client, h, "1.txt", b"one")
    upload(client, h, "2.txt", b"two")
    calls = []
    monkeypatch.setattr(llm, "build_profile", lambda **kw: calls.append(1) or {"name": "X"})
    r = upload(client, h, "3.txt", b"three")
    assert r.status_code == 409 and "DELETE /resumes/{id}" in r.json()["detail"]
    assert calls == []                                                          # no LLM tokens spent
    assert len(client.get("/resumes", headers=h).json()) == 2
    assert upload(client, h, "1-again.txt", b"one").status_code == 200          # duplicate is not "new"
    ids = [i["id"] for i in client.get("/resumes", headers=h).json()]
    assert client.delete(f"/resumes/{ids[-1]}", headers=h).status_code == 204
    assert upload(client, h, "3.txt", b"three").status_code == 200
    hb = signup(client, "b@example.com")
    assert upload(client, hb, "b.txt", b"mine").status_code == 200             # limit is per user


def test_delete_removes_one_file_but_keeps_profile_and_jobs(client, h):
    upload(client, h, "a.txt", b"Python engineer")
    upload(client, h, "b.txt", b"Go engineer")
    client.post("/search", headers=h)
    newest, older = client.get("/resumes", headers=h).json()
    assert client.delete(f"/resumes/{newest['id']}", headers=h).status_code == 204
    assert client.delete(f"/resumes/{newest['id']}", headers=h).status_code == 404
    assert client.get("/resume", headers=h).content == b"Python engineer"      # falls back to the previous
    assert client.delete(f"/resumes/{older['id']}", headers=h).status_code == 204
    assert client.get("/resume", headers=h).status_code == 404
    prof = client.get("/profile", headers=h).json()
    assert prof["resume"] is None and prof["profile"]["name"] == "Test User"
    assert client.get("/jobs?min_score=0", headers=h).json()["total"] == 2


def test_reextract_rebuilds_the_profile_from_the_stored_file(client, h, monkeypatch):
    seen = []

    def fake_build(resume_bytes=None, resume_text=None, is_pdf=False, **kw):
        seen.append((resume_bytes, resume_text, is_pdf))
        return {"name": "Second Pass", "years_experience": 5, "core_skills": ["Go"]}

    upload(client, h, "cv.txt", b"Python engineer")
    client.put("/profile", headers=h, json={"years_experience": 9})        # a manual correction
    monkeypatch.setattr(llm, "build_profile", fake_build)
    r = client.post("/resume/reextract", headers=h)
    assert r.status_code == 200
    assert r.json()["profile"]["name"] == "Second Pass"
    assert r.json()["profile"]["years_experience"] == 5                   # correction overwritten, as documented
    assert seen == [(None, "Python engineer", False)]
    assert r.json()["source_file"] == "cv.txt" and r.json()["resume"]["filename"] == "cv.txt"

    upload(client, h, "cv.pdf", PDF)
    client.post("/resume/reextract", headers=h)
    assert seen[-1] == (PDF, None, True)


def test_reextract_failure_leaves_profile_and_file_alone(client, h, monkeypatch):
    upload(client, h)

    def boom(**kw):
        raise LLMError("503")
    monkeypatch.setattr(llm, "build_profile", boom)
    assert client.post("/resume/reextract", headers=h).status_code == 502
    assert client.get("/profile", headers=h).json()["profile"]["name"] == "Test User"
    assert client.get("/resume", headers=h).status_code == 200


@pytest.mark.parametrize("sent, expected", [
    ("../../etc/passwd.txt", "passwd.txt"),
    ("C:\\Users\\x\\cv.pdf", "cv.pdf"),
    ('we"ird<na>me.txt', "weirdname.txt"),
    ("tab\tname\r\n.txt", "tabname.txt"),
    ("  .hidden.md  ", "hidden.md"),
    ("", "resume"),
    (None, "resume"),
    ("x" * 400 + ".pdf", None),
])
def test_filename_sanitiser(sent, expected):
    got = app_module._safe_filename(sent)
    if expected is None:
        assert len(got) <= 150
    else:
        assert got == expected
    assert "/" not in got and "\\" not in got and got.isprintable()


def test_path_traversal_filename_is_stored_as_a_plain_label(client, h):
    r = upload(client, h, "../../etc/passwd.txt", b"Python engineer")
    assert r.status_code == 200 and r.json()["resume"]["filename"] == "passwd.txt"
    assert "passwd.txt" in client.get("/resume", headers=h).headers["content-disposition"]


def test_header_injection_in_filename_cannot_split_the_response(client, h):
    upload(client, h, "a%0d%0aSet-Cookie: x=1.txt", b"Python engineer")
    r = client.get("/resume", headers=h)
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_unicode_filenames_download_cleanly(client, h):
    upload(client, h, "résumé Naveen.txt", b"Python engineer")
    cd = client.get("/resume", headers=h).headers["content-disposition"]
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20Naveen.txt" in cd
    cd.encode("ascii")                                               # header itself is plain ASCII


def test_users_cannot_download_or_delete_each_others_resume(client, h):
    hb = signup(client, "b@example.com")
    upload(client, h, "a.pdf", PDF)
    a_id = client.get("/resumes", headers=h).json()[0]["id"]
    assert client.get("/resume", headers=hb).status_code == 404
    assert client.get("/resumes", headers=hb).json() == []
    assert client.get(f"/resumes/{a_id}", headers=hb).status_code == 404
    assert client.delete(f"/resumes/{a_id}", headers=hb).status_code == 404
    upload(client, hb, "b.txt", b"Python engineer")
    assert client.get("/resume", headers=h).content == PDF
    assert client.get("/resume", headers=hb).content == b"Python engineer"
    assert client.get(f"/resumes/{a_id}", headers=h).content == PDF            # still there for the owner
    upload(client, hb, "a-copy.pdf", PDF)                                       # same bytes, different user
    assert len(client.get("/resumes", headers=h).json()) == 1
    assert len(client.get("/resumes", headers=hb).json()) == 2


def test_resumes_are_deleted_with_the_account(client, h):
    upload(client, h, "a.txt", b"one")
    upload(client, h, "b.txt", b"two")
    sql(client, "DELETE FROM users")
    with client.app.state.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0


def test_existing_database_gains_the_resumes_table_without_losing_data(tmp_path):
    path = tmp_path / "x.db"
    db = Database(path)
    db.init()
    uid = db.create_user("a@example.com", "h")["id"]
    db.save_profile(uid, {"name": "Keep Me"}, "cv.pdf")
    with db.connect() as c:
        c.execute("DROP TABLE resumes")                    # a database from before this feature
    Database(path).init()
    assert Database(path).get_profile(uid)["profile"]["name"] == "Keep Me"
    assert Database(path).get_profile(uid)["resume"] is None


def test_single_resume_table_is_migrated_into_the_history_table(tmp_path):
    path = tmp_path / "x.db"
    db = Database(path)
    db.init()
    a = db.create_user("a@example.com", "h")["id"]
    b = db.create_user("b@example.com", "h")["id"]
    with db.connect() as c:                                # the shape the previous version created
        c.execute("DROP TABLE resumes")
        c.execute("CREATE TABLE resumes (user_id INTEGER PRIMARY KEY REFERENCES users(id) "
                  "ON DELETE CASCADE, filename TEXT NOT NULL, media_type TEXT NOT NULL, "
                  "size INTEGER NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL, "
                  "uploaded_at TEXT NOT NULL)")
        for uid, name, data in [(a, "a.pdf", PDF), (b, "b.txt", b"hello")]:
            c.execute("INSERT INTO resumes VALUES (?, ?, 'x/y', ?, 'h', ?, '2026-01-01T00:00:00+00:00')",
                      (uid, name, len(data), data))
    db = Database(path)
    db.init()
    db.init()                                              # idempotent
    assert [r["filename"] for r in db.list_resumes(a)] == ["a.pdf"]
    assert db.get_resume(b)["data"] == b"hello"
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='resumes_single'").fetchone()
    db.save_profile(a, {"name": "A"}, "n.txt", resume={"filename": "n.txt", "media_type": "text/plain",
                                                       "data": b"newer"})
    assert [r["filename"] for r in db.list_resumes(a)] == ["n.txt", "a.pdf"]       # old one kept
