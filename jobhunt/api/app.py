"""FastAPI app: accounts, resume upload, profile, and the job-hunt run.

    POST /auth/register   create an account
    POST /auth/login      email + password -> JWT (form-encoded, OAuth2 password flow)
    POST /auth/logout     end the session;  GET /auth/me  who am I
    POST /resume          upload a PDF/TXT/MD -> extracts your profile; every upload is kept
    GET /resume           download the newest resume;  GET /resumes lists all of them
    GET|DELETE /resumes/{id}   download or delete one of them
    POST /resume/reextract     rebuild the profile from the newest stored resume
    GET/PUT /profile      read or correct the stored profile
    POST /study-plan      store study plan requirements;  GET /study-plan the latest
    POST /search          start a background run (202); GET /runs/{id} to follow it
    GET /jobs             scored matches, filterable; GET /jobs/{id} for the full kit
    POST /jobs/{id}/applied

Everything except /health and /auth/register|login needs `Authorization: Bearer <jwt>`.
Sessions end after SESSION_IDLE_MINUTES (30) without a request, whatever the JWT's
own expiry says; SESSION_MAX_HOURS (12) caps a session however active it is.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import jwt
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Response, UploadFile
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import ValidationError

from .. import llm
from .. import skills as skills_mod
from .. import studyplan
from ..pipeline import PipelineError, run_pipeline
from ..providers import LLMError, resolve
from .auth import (AuthNotConfigured, AuthSettings, burn_password_check, create_token,
                   decode_token, hash_password, load_settings, verify_password)
from .db import Database, RunBlocked
from .schemas import (JobDetail, JobList, MeOut, Profile, ProfileOut, ProfileUpdate,
                      RegisterRequest, ResumeListItem, RunOut, SearchRequest, SkillInsights,
                      StudyPlanRequirement, StudyPlanOut, TokenOut, UserOut)

log = logging.getLogger("jobhunt.api")
ROOT = Path(__file__).resolve().parent.parent.parent

RESUME_EXTENSIONS = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
                     ".md": "text/markdown; charset=utf-8"}
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_PASSWORD_LEN = 128
INSIGHTS_MAX_JOBS = 2000
BEARER = {"WWW-Authenticate": "Bearer"}

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _safe_filename(raw: str | None) -> str:
    """Client-supplied names are only labels: no paths, no control characters."""
    name = Path((raw or "resume").replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"<>|:*?').strip(" .")
    return name[:150] or "resume"


def _attachment(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def create_app(db_path: str | Path | None = None, config_path: str | Path | None = None) -> FastAPI:
    db = Database(db_path or os.getenv("DB_PATH", ROOT / "data" / "jobhunt.db"))
    config_file = Path(config_path or os.getenv("JOBHUNT_CONFIG", ROOT / "config.yaml"))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init()
        try:
            load_settings()
        except AuthNotConfigured as e:
            log.warning("%s: sign-in is disabled (503) until it is", e)
        yield

    origins = [
        "http://localhost:5173",
        "https://naveenrawat19.github.io/MyPlanner/",
    ]

    app = FastAPI(title="jobhunt", version="2.0", lifespan=lifespan)
    app.state.db = db
    plan_slots = threading.BoundedSemaphore(max(1, _env_int("MAX_CONCURRENT_PLANS", 2)))

    def load_cfg() -> dict:
        if not config_file.exists():
            raise HTTPException(500, f"config not found: {config_file.name}")
        return yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}

    def load_companies(cfg: dict) -> list[dict]:
        path = Path(cfg.get("companies_file", "companies.yaml"))
        path = path if path.is_absolute() else config_file.parent / path
        if not path.exists():
            raise HTTPException(500, f"companies file not found: {path.name}")
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("companies") or []

    # --------------------------------------------------------------------- auth
    def auth_settings() -> AuthSettings:
        try:
            return load_settings()
        except AuthNotConfigured:
            raise HTTPException(503, "server auth is not configured (JWT_SECRET)")

    def current_user(token: str = Depends(oauth2_scheme),
                     settings: AuthSettings = Depends(auth_settings)) -> dict:
        try:
            claims = decode_token(settings, token)
            user_id = int(claims["sub"])
            session_id = str(claims["sid"])
        except jwt.ExpiredSignatureError:
            raise HTTPException(401, "session expired", headers=BEARER)
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, "invalid token", headers=BEARER)

        # This is the inactivity timeout: the JWT alone would stay valid for
        # SESSION_MAX_HOURS, so every request must also find a recently-used session.
        user = db.touch_session(session_id, user_id, settings.idle_seconds)
        if not user:
            raise HTTPException(401, "session expired", headers=BEARER)
        return {**user, "session_id": session_id}

    @app.post("/auth/register", response_model=UserOut, status_code=201, tags=["auth"])
    def register(body: RegisterRequest, _: AuthSettings = Depends(auth_settings)) -> dict:
        if os.getenv("ALLOW_REGISTRATION", "true").lower() in ("0", "false", "no"):
            raise HTTPException(403, "registration is closed")
        user = db.create_user(body.email.strip().lower(), hash_password(body.password))
        if not user:
            raise HTTPException(409, "an account with this email already exists")
        return user

    @app.post("/auth/login", response_model=TokenOut, tags=["auth"])
    def login(form: OAuth2PasswordRequestForm = Depends(),
              settings: AuthSettings = Depends(auth_settings)) -> dict:
        """Form fields `username` (your email) and `password`. Send the returned
        token as `Authorization: Bearer <token>`."""
        bad = HTTPException(401, "incorrect email or password", headers=BEARER)
        user = db.get_user_by_email(form.username.strip().lower())

        if user:
            left = db.lock_remaining(user)
            if left:
                raise HTTPException(429, "too many failed attempts; try again later",
                                    headers={"Retry-After": str(left)})
        if len(form.password) > MAX_PASSWORD_LEN:
            raise bad
        if not user:
            burn_password_check(form.password)  # same latency as a wrong password
            raise bad
        if not verify_password(form.password, user["password_hash"]):
            db.record_failed_login(user["id"], settings.max_failed_logins, settings.lockout_seconds)
            raise bad

        if user["failed_attempts"]:
            db.reset_failed_logins(user["id"])
        token, exp = create_token(settings, user["id"], db.create_session(user["id"]))
        return {"access_token": token, "token_type": "bearer",
                "idle_timeout_seconds": settings.idle_seconds,
                "has_profile": db.get_profile(user["id"]) is not None,
                "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds")}

    @app.post("/auth/logout", status_code=204, tags=["auth"])
    def logout(user: dict = Depends(current_user)) -> None:
        db.revoke_session(user["session_id"])

    @app.get("/auth/me", response_model=MeOut, tags=["auth"])
    def me(user: dict = Depends(current_user),
           settings: AuthSettings = Depends(auth_settings)) -> dict:
        """Also a cheap keep-alive: any authenticated call resets the idle timer."""
        return {**user, "idle_timeout_seconds": settings.idle_seconds,
                "has_profile": db.get_profile(user["id"]) is not None}

    # ------------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ----------------------------------------------------------------- profile
    def extract_profile(data: bytes, ext: str) -> Profile:
        is_pdf = ext == ".pdf"
        try:
            provider, model = resolve("draft")
            raw = llm.build_profile(
                resume_bytes=data if is_pdf else None,
                resume_text=None if is_pdf else data.decode("utf-8", errors="replace"),
                is_pdf=is_pdf, provider=provider, model=model)
            return Profile.model_validate(raw)
        except LLMError as e:
            raise HTTPException(502, f"resume extraction failed: {e}")
        except (ValueError, ValidationError) as e:
            raise HTTPException(502, f"model returned an unusable profile: {e}")

    @app.post("/resume", response_model=ProfileOut, tags=["resume"])
    def upload_resume(file: UploadFile, user: dict = Depends(current_user)) -> dict:
        """Upload a resume (.pdf, .txt or .md, max 5 MB). The profile is extracted and
        saved, and the file is kept as a new version: earlier uploads stay available
        under GET /resumes. Uploading identical bytes again does not add a duplicate."""
        name = _safe_filename(file.filename)
        ext = Path(name).suffix.lower()
        if ext not in RESUME_EXTENSIONS:
            raise HTTPException(415, f"unsupported file type {ext or '(none)'}; use .pdf, .txt or .md")
        data = file.file.read(MAX_RESUME_BYTES + 1)
        if len(data) > MAX_RESUME_BYTES:
            raise HTTPException(413, "resume is larger than 5 MB")
        if not data.strip():
            raise HTTPException(422, "resume file is empty")
        if ext == ".pdf" and not data.startswith(b"%PDF"):
            raise HTTPException(415, "file is not a valid PDF")
        # Checked before extraction so a refused upload does not spend LLM tokens.
        limit = _env_int("MAX_RESUMES_PER_USER", 50)
        if (db.count_resumes(user["id"]) >= limit
                and not db.has_resume_hash(user["id"], hashlib.sha256(data).hexdigest())):
            raise HTTPException(409, f"you already have {limit} stored resumes; "
                                     "delete some with DELETE /resumes/{id}")

        profile = extract_profile(data, ext)
        # Stored only after extraction succeeds, so a failed upload adds nothing.
        return db.save_profile(user["id"], profile.model_dump(), name,
                               resume={"filename": name, "media_type": RESUME_EXTENSIONS[ext],
                                       "data": data})

    def resume_response(stored: dict | None) -> Response:
        if not stored:
            raise HTTPException(404, "no such resume; upload one with POST /resume")
        return Response(
            stored["data"], media_type=stored["media_type"],
            headers={"Content-Disposition": _attachment(stored["filename"]),
                     "X-Content-Type-Options": "nosniff",
                     "Cache-Control": "private, no-store"})

    download_docs = {200: {"content": {"application/pdf": {}, "text/plain": {}, "text/markdown": {}},
                           "description": "The resume file, as an attachment."}}

    @app.get("/resume", tags=["resume"], response_class=Response, responses=download_docs)
    def download_resume(user: dict = Depends(current_user)) -> Response:
        """The newest uploaded resume."""
        return resume_response(db.get_resume(user["id"]))

    @app.get("/resumes", response_model=list[ResumeListItem], tags=["resume"])
    def list_resumes(user: dict = Depends(current_user)) -> list[dict]:
        """Every resume you have uploaded, newest first (metadata only)."""
        items = db.list_resumes(user["id"])
        return [{**r, "is_current": i == 0} for i, r in enumerate(items)]

    @app.get("/resumes/{resume_id}", tags=["resume"], response_class=Response,
             responses=download_docs)
    def download_resume_version(resume_id: int, user: dict = Depends(current_user)) -> Response:
        return resume_response(db.get_resume(user["id"], resume_id))

    @app.delete("/resumes/{resume_id}", status_code=204, tags=["resume"])
    def delete_resume(resume_id: int, user: dict = Depends(current_user)) -> None:
        """Delete one stored resume. The extracted profile and your jobs are kept."""
        if not db.delete_resume(user["id"], resume_id):
            raise HTTPException(404, "no such resume")

    @app.post("/resume/reextract", response_model=ProfileOut, tags=["resume"])
    def reextract_resume(user: dict = Depends(current_user)) -> dict:
        """Re-run profile extraction on the newest stored resume, e.g. after switching model.
        This overwrites the profile, including any corrections made with PUT /profile."""
        stored = db.get_resume(user["id"])
        if not stored:
            raise HTTPException(404, "no stored resume; upload one with POST /resume")
        profile = extract_profile(stored["data"], Path(stored["filename"]).suffix.lower())
        return db.save_profile(user["id"], profile.model_dump(), stored["filename"])

    @app.get("/profile", response_model=ProfileOut)
    def get_profile(user: dict = Depends(current_user)) -> dict:
        stored = db.get_profile(user["id"])
        if not stored:
            raise HTTPException(404, "no profile yet; POST /resume first")
        return stored

    @app.put("/profile", response_model=ProfileOut)
    def update_profile(update: ProfileUpdate, user: dict = Depends(current_user)) -> dict:
        """Correct fields the extraction got wrong (e.g. years_experience)."""
        stored = db.get_profile(user["id"])
        if not stored:
            raise HTTPException(404, "no profile yet; POST /resume first")
        merged = {**stored["profile"], **update.model_dump(exclude_unset=True, exclude_none=True)}
        return db.save_profile(user["id"], Profile.model_validate(merged).model_dump(), None)

    # -------------------------------------------------------------- study plan
    @app.post("/study-plan", response_model=StudyPlanOut, status_code=201, tags=["study plan"])
    def create_study_plan(body: StudyPlanRequirement, user: dict = Depends(current_user)) -> dict:
        """Store the requirements and generate the plan with the draft-stage LLM.

        `plan` is plain text in the planner app's import format. Long plans are built
        in several model calls, so expect this to take a minute or more."""
        data = body.model_dump()
        days = studyplan.plan_days(data)
        max_days = _env_int("MAX_PLAN_DAYS", 120)
        if days > max_days:
            raise HTTPException(422, f"a plan can cover at most {max_days} days; this asks for {days}")
        try:
            provider, model = resolve("draft")
        except LLMError as e:
            raise HTTPException(503, f"plan generation is not configured: {e}")
        if not plan_slots.acquire(blocking=False):
            raise HTTPException(503, "too many plans are being generated; try again shortly",
                                headers={"Retry-After": "30"})
        try:
            stored = db.save_study_plan(user["id"], data)
            try:
                stored["plan"] = studyplan.generate_plan(data, provider, model)
            except LLMError as e:
                raise HTTPException(502, f"plan generation failed ({e}); your requirements "
                                         f"were saved as study plan {stored['id']}")
            db.set_study_plan_text(user["id"], stored["id"], stored["plan"])
            return stored
        finally:
            plan_slots.release()

    @app.get("/study-plan", response_model=StudyPlanOut, tags=["study plan"])
    def get_study_plan(user: dict = Depends(current_user)) -> dict:
        """The most recently stored requirements and plan."""
        stored = db.latest_study_plan(user["id"])
        if not stored:
            raise HTTPException(404, "no study plan requirements yet; POST /study-plan first")
        return stored

    # ------------------------------------------------------------------ search
    def execute_run(run_id: str, user_id: int, profile: dict, cfg: dict, companies: list[dict],
                    req: SearchRequest) -> None:
        try:
            limit = req.limit or _env_int("MAX_SCREEN_JOBS", 80)
            result = run_pipeline(
                profile, cfg, db.seen_ids(user_id), companies, draft=req.draft, limit=limit,
                progress=lambda stage, msg: db.update_run(run_id, stage, msg))
            db.save_jobs(user_id, result.jobs, run_id)
            db.finish_run(run_id, warnings=result.warnings, counts={
                "scanned": result.scanned, "passed_filters": result.passed_filters,
                "new": result.candidates, "screened": len(result.jobs),
                "matches": len(result.shortlist)})
        except PipelineError as e:
            db.finish_run(run_id, error=str(e))
        except Exception as e:  # never leave the run stuck on "running"
            log.exception("run %s crashed", run_id)
            db.finish_run(run_id, error=f"internal error: {type(e).__name__}")

    @app.post("/search", response_model=RunOut, status_code=202)
    def search(background: BackgroundTasks, req: SearchRequest | None = None,
               user: dict = Depends(current_user)) -> dict:
        """Start a run in the background and return immediately. One run at a time
        per user (409), at most one per MIN_RUN_INTERVAL_MIN minutes (429), and at
        most MAX_CONCURRENT_RUNS across all users (503)."""
        req = req or SearchRequest()
        stored = db.get_profile(user["id"])
        if not stored:
            raise HTTPException(409, "upload a resume first (POST /resume)")
        cfg = load_cfg()
        companies = load_companies(cfg)
        if req.companies:
            wanted = {c.lower() for c in req.companies}
            names = {(c.get("name") or c.get("slug") or "").lower() for c in companies}
            unknown = sorted(wanted - names)
            if unknown:
                raise HTTPException(422, f"unknown companies: {', '.join(unknown)}")
            companies = [c for c in companies
                         if (c.get("name") or c.get("slug") or "").lower() in wanted]

        try:
            run = db.start_run(user["id"], req.model_dump(),
                               _env_int("MIN_RUN_INTERVAL_MIN", 30) * 60,
                               _env_int("MAX_CONCURRENT_RUNS", 3))
        except RunBlocked as e:
            if e.kind == "running":
                raise HTTPException(409, f"a run is already in progress: {e.run_id}")
            if e.kind == "busy":
                raise HTTPException(503, "server is busy; try again shortly",
                                    headers={"Retry-After": str(e.retry_after)})
            raise HTTPException(429, "last run was too recent",
                                headers={"Retry-After": str(e.retry_after)})
        background.add_task(execute_run, run["run_id"], user["id"], stored["profile"], cfg,
                            companies, req)
        return run

    @app.get("/runs/latest", response_model=RunOut)
    def latest_run(user: dict = Depends(current_user)) -> dict:
        run = db.latest_run(user["id"])
        if not run:
            raise HTTPException(404, "no runs yet")
        return run

    @app.get("/runs/{run_id}", response_model=RunOut)
    def get_run(run_id: str, user: dict = Depends(current_user)) -> dict:
        run = db.get_run(user["id"], run_id)
        if not run:
            raise HTTPException(404, "unknown run")
        return run

    # -------------------------------------------------------------------- jobs
    @app.get("/jobs", response_model=JobList)
    def list_jobs(
        min_score: float | None = Query(None, ge=0, le=10,
                                        description="Defaults to score_threshold in config.yaml; use 0 for everything."),
        company: str | None = None,
        q: str | None = Query(None, description="Substring of title or company."),
        location: str | None = Query(None, description="Substring of location."),
        status: str | None = Query(None, pattern="^(new|applied)$"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user: dict = Depends(current_user),
    ) -> dict:
        if min_score is None:
            min_score = float(load_cfg().get("score_threshold", 7.0))
        total, items = db.list_jobs(user["id"], min_score=min_score, company=company, q=q,
                                    location=location, status=status, limit=limit, offset=offset)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @app.get("/jobs/{job_id}", response_model=JobDetail)
    def get_job(job_id: str, user: dict = Depends(current_user)) -> dict:
        job = db.get_job(user["id"], job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        return job

    @app.post("/jobs/{job_id}/applied", response_model=JobDetail)
    def mark_applied(job_id: str, user: dict = Depends(current_user)) -> dict:
        if not db.mark_applied(user["id"], job_id):
            raise HTTPException(404, "unknown job")
        return db.get_job(user["id"], job_id)  # type: ignore[return-value]

    @app.get("/stats")
    def stats(user: dict = Depends(current_user)) -> dict:
        return db.stats(user["id"])

    @app.get("/insights/skills", response_model=SkillInsights, tags=["insights"])
    def skill_insights(
        role: str | None = Query(None, description=f"Role family: {', '.join(skills_mod.ROLES)}."),
        category: str | None = Query(None, description=f"Only one kind: {', '.join(skills_mod.CATEGORIES)}."),
        min_score: float = Query(0, ge=0, le=10, description="0 = every stored posting, not just matches."),
        company: str | None = None,
        q: str | None = Query(None, description="Substring of title or company."),
        location: str | None = Query(None, description="Substring of location."),
        top: int = Query(15, ge=1, le=100),
        user: dict = Depends(current_user),
    ) -> dict:
        """What the postings found by your searches ask for, as counts and percentages.
        Deterministic keyword matching over the stored descriptions, so every number
        comes from real postings; run /search more to grow the sample."""
        if role and role not in skills_mod.ROLES:
            raise HTTPException(422, f"unknown role {role!r}; use one of {', '.join(skills_mod.ROLES)}")
        if category and category not in skills_mod.CATEGORIES:
            raise HTTPException(422, f"unknown category {category!r}; use one of {', '.join(skills_mod.CATEGORIES)}")
        cap = INSIGHTS_MAX_JOBS
        rows = db.postings_for_insights(user["id"], min_score=min_score, company=company,
                                        q=q, location=location, cap=cap + 1)
        truncated = len(rows) > cap
        rows = rows[:cap]
        role_counts = Counter(skills_mod.role_of(r["title"]) for r in rows)
        if role:
            rows = [r for r in rows if skills_mod.role_of(r["title"]) == role]

        n, counts = skills_mod.count_skills(f"{r['title']}\n{r['description']}" for r in rows)
        stored = db.get_profile(user["id"])
        have = skills_mod.extract(" ; ".join(
            [*stored["profile"]["core_skills"], *stored["profile"]["domains"]])) if stored else None

        ranked = [(name, c) for name, c in counts.most_common()
                  if not category or skills_mod.BY_NAME[name].category == category]
        stats = [{"skill": name, "category": skills_mod.BY_NAME[name].category, "jobs": c,
                  "percent": round(100 * c / n, 1), "in_profile": None if have is None else name in have}
                 for name, c in ranked[:top]]
        gaps = [] if have is None else [s["skill"] for s in stats if not s["in_profile"]][:10]
        with_desc = sum(1 for r in rows if r["description"].strip())
        note = None
        if n == 0:
            note = "no stored postings match; run POST /search first or loosen the filters"
        elif n < 20:
            note = f"small sample ({n} postings); percentages will move a lot as you run more searches"
        elif with_desc < n * 0.8:
            note = "many postings have no description text, so percentages are understated"
        return {"sample_size": n, "with_description": with_desc, "truncated": truncated, "note": note,
                "roles": [{"role": r, "jobs": role_counts[r]} for r in skills_mod.ROLES if role_counts[r]],
                "skills": stats, "gaps": gaps}

    return app


app = create_app()
