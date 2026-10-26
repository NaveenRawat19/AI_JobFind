"""SQLite storage for the API: users, sessions, and each user's profile, runs and jobs.

Every method opens its own short-lived connection, so request threads and the
background run never share one. All user data is scoped by user_id.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..fetch import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profile (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data        TEXT NOT NULL,
    source_file TEXT,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    data        BLOB NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resumes_user ON resumes (user_id, id DESC);
CREATE TABLE IF NOT EXISTS study_plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    plan       TEXT
);
CREATE INDEX IF NOT EXISTS idx_study_plans_user ON study_plans (user_id, id DESC);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,          -- running | succeeded | failed
    stage       TEXT,
    message     TEXT,
    options     TEXT,
    counts      TEXT,
    warnings    TEXT,
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs (user_id, started_at DESC);
CREATE TABLE IF NOT EXISTS jobs (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id      TEXT NOT NULL,
    ats         TEXT,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    url         TEXT,
    description TEXT,
    posted_at   TEXT,
    salary      TEXT,
    score       REAL,
    reason      TEXT,
    draft       TEXT,
    run_id      TEXT,
    first_seen  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',   -- new | applied
    applied_on  TEXT,
    PRIMARY KEY (user_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs (user_id, score DESC, first_seen DESC);
"""


class RunBlocked(Exception):
    """A new run may not start: one is going, the last was too recent, or the server is busy."""

    def __init__(self, kind: str, run_id: str | None = None, retry_after: int = 0):
        super().__init__(kind)
        self.kind, self.run_id, self.retry_after = kind, run_id, retry_after


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return _fmt(datetime.now(timezone.utc))


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None: autocommit, so transactions are explicit where needed.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._retire_single_user_db()
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            self._set_aside_single_resume_table(c)
            c.executescript(SCHEMA)
            if "plan" not in {r["name"] for r in c.execute("PRAGMA table_info(study_plans)")}:
                c.execute("ALTER TABLE study_plans ADD COLUMN plan TEXT")
            self._restore_single_resume_rows(c)
            # A restart kills the worker thread; its run would otherwise stay
            # "running" forever and block every future search.
            c.execute("UPDATE runs SET status='failed', error='server restarted during run', "
                      "finished_at=? WHERE status='running'", (_now(),))

    def _set_aside_single_resume_table(self, c: sqlite3.Connection) -> None:
        """The first resume feature kept one file per user (user_id as primary key).
        Rename that table so the history schema can be created beside it."""
        cols = [r[1] for r in c.execute("PRAGMA table_info(resumes)")]
        if cols and "id" not in cols:
            c.execute("ALTER TABLE resumes RENAME TO resumes_single")

    def _restore_single_resume_rows(self, c: sqlite3.Connection) -> None:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumes_single'").fetchone():
            return
        c.execute("BEGIN")
        c.execute("INSERT INTO resumes (user_id, filename, media_type, size, sha256, data, uploaded_at) "
                  "SELECT user_id, filename, media_type, size, sha256, data, uploaded_at "
                  "FROM resumes_single ORDER BY uploaded_at")
        c.execute("DROP TABLE resumes_single")
        c.execute("COMMIT")

    def _retire_single_user_db(self) -> None:
        """Pre-auth databases have no user_id column and no owner to assign it to.
        Move the file aside rather than fail or delete it."""
        if not self.path.exists():
            return
        conn = sqlite3.connect(self.path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        finally:
            conn.close()
        if cols and "user_id" not in cols:
            backup = self.path.with_suffix(".pre-auth.bak")
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self.path) + suffix)
                if src.exists():
                    src.replace(Path(str(backup) + suffix))

    # ------------------------------------------------------------------ users
    def create_user(self, email: str, password_hash: str) -> dict | None:
        """None if the email is already registered."""
        try:
            with self.connect() as c:
                cur = c.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                                (email, password_hash, _now()))
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return None
        return self.get_user(user_id)

    @staticmethod
    def _user(row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        return {"id": row["id"], "email": row["email"], "created_at": row["created_at"],
                "password_hash": row["password_hash"], "failed_attempts": row["failed_attempts"],
                "locked_until": row["locked_until"]}

    def get_user(self, user_id: int) -> dict | None:
        with self.connect() as c:
            return self._user(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def get_user_by_email(self, email: str) -> dict | None:
        with self.connect() as c:
            return self._user(c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone())

    def lock_remaining(self, user: dict) -> int:
        """Seconds left on a login lockout. Clears an expired lockout as a side effect."""
        until = user.get("locked_until")
        if not until:
            return 0
        left = (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds()
        if left > 0:
            return int(left) + 1
        self.reset_failed_logins(user["id"])
        return 0

    def record_failed_login(self, user_id: int, max_attempts: int, lockout_s: int) -> None:
        locked_until = _fmt(datetime.now(timezone.utc) + timedelta(seconds=lockout_s))
        with self.connect() as c:
            c.execute("UPDATE users SET failed_attempts = failed_attempts + 1, "
                      "locked_until = CASE WHEN failed_attempts + 1 >= ? THEN ? ELSE locked_until END "
                      "WHERE id=?", (max_attempts, locked_until, user_id))

    def reset_failed_logins(self, user_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (user_id,))

    # --------------------------------------------------------------- sessions
    def create_session(self, user_id: int) -> str:
        sid, now = uuid.uuid4().hex, _now()
        with self.connect() as c:
            c.execute("INSERT INTO sessions (id, user_id, created_at, last_activity) VALUES (?,?,?,?)",
                      (sid, user_id, now, now))
            # housekeeping: nothing older than a week can still be valid
            c.execute("DELETE FROM sessions WHERE last_activity < ?",
                      (_fmt(datetime.now(timezone.utc) - timedelta(days=7)),))
        return sid

    def touch_session(self, session_id: str, user_id: int, idle_seconds: int) -> dict | None:
        """Slide the inactivity window forward and return the user, or None if the
        session was revoked, belongs to someone else, or has been idle too long.
        The check and the update are one statement, so they cannot race."""
        now = datetime.now(timezone.utc)
        with self.connect() as c:
            cur = c.execute(
                "UPDATE sessions SET last_activity=? WHERE id=? AND user_id=? AND revoked=0 "
                "AND last_activity >= ?",
                (_fmt(now), session_id, user_id, _fmt(now - timedelta(seconds=idle_seconds))))
            if cur.rowcount == 0:
                return None
            return self._user(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def revoke_session(self, session_id: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE sessions SET revoked=1 WHERE id=?", (session_id,))

    # ---------------------------------------------------------------- profile
    def save_profile(self, user_id: int, profile: dict, source_file: str | None,
                     resume: dict | None = None) -> dict:
        """Store the profile; with `resume` (filename, media_type, data) also store the
        file, in the same transaction so a profile never points at a missing upload."""
        with self.connect() as c:
            c.execute("BEGIN")
            c.execute(
                "INSERT INTO profile (user_id, data, source_file, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, "
                "source_file=COALESCE(excluded.source_file, profile.source_file), "
                "updated_at=excluded.updated_at",
                (user_id, json.dumps(profile, ensure_ascii=False), source_file, _now()))
            if resume:
                data = resume["data"]
                digest = hashlib.sha256(data).hexdigest()
                # The same bytes uploaded again are not a new version: move the copy to the top.
                c.execute("DELETE FROM resumes WHERE user_id = ? AND sha256 = ?", (user_id, digest))
                c.execute(
                    "INSERT INTO resumes (user_id, filename, media_type, size, sha256, data, uploaded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, resume["filename"], resume["media_type"], len(data),
                     digest, data, _now()))
            c.execute("COMMIT")
        return self.get_profile(user_id)  # type: ignore[return-value]

    def get_profile(self, user_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return {"profile": json.loads(row["data"]), "source_file": row["source_file"],
                "updated_at": row["updated_at"], "resume": self.resume_info(user_id)}

    # ------------------------------------------------------------- study plans
    def save_study_plan(self, user_id: int, data: dict) -> dict:
        created = _now()
        with self.connect() as c:
            cur = c.execute("INSERT INTO study_plans (user_id, data, created_at) VALUES (?, ?, ?)",
                            (user_id, json.dumps(data, ensure_ascii=False), created))
        return {**data, "id": cur.lastrowid, "created_at": created, "plan": None}

    def set_study_plan_text(self, user_id: int, plan_id: int, plan: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE study_plans SET plan = ? WHERE id = ? AND user_id = ?",
                      (plan, plan_id, user_id))

    def latest_study_plan(self, user_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM study_plans WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                            (user_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["data"]), "id": row["id"], "created_at": row["created_at"],
                "plan": row["plan"]}

    # ----------------------------------------------------------------- resume
    _INFO_COLS = "id, filename, media_type, size, sha256, uploaded_at"

    def resume_info(self, user_id: int) -> dict | None:
        """Metadata of the newest resume; the bytes are never loaded here."""
        with self.connect() as c:
            row = c.execute(f"SELECT {self._INFO_COLS} FROM resumes WHERE user_id = ? "
                            "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_resumes(self, user_id: int) -> list[dict]:
        """Every resume the user has uploaded, newest first, without the bytes."""
        with self.connect() as c:
            rows = c.execute(f"SELECT {self._INFO_COLS} FROM resumes WHERE user_id = ? "
                             "ORDER BY id DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def count_resumes(self, user_id: int) -> int:
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) FROM resumes WHERE user_id = ?", (user_id,)).fetchone()[0]

    def has_resume_hash(self, user_id: int, sha256: str) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM resumes WHERE user_id = ? AND sha256 = ?",
                             (user_id, sha256)).fetchone() is not None

    def get_resume(self, user_id: int, resume_id: int | None = None) -> dict | None:
        """One resume with its bytes: the given id, or the newest. Scoped to the user."""
        with self.connect() as c:
            if resume_id is None:
                row = c.execute("SELECT * FROM resumes WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                                (user_id,)).fetchone()
            else:
                row = c.execute("SELECT * FROM resumes WHERE user_id = ? AND id = ?",
                                (user_id, resume_id)).fetchone()
        return dict(row) if row else None

    def delete_resume(self, user_id: int, resume_id: int) -> bool:
        with self.connect() as c:
            return c.execute("DELETE FROM resumes WHERE user_id = ? AND id = ?",
                             (user_id, resume_id)).rowcount > 0

    # ------------------------------------------------------------------- runs
    def start_run(self, user_id: int, options: dict, min_interval_s: int,
                  max_concurrent: int) -> dict:
        """Create a run row, or raise RunBlocked. One run per user at a time,
        rate-limited per user, and a cap on runs across all users."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")  # check-then-insert must be atomic
            try:
                running = c.execute("SELECT id FROM runs WHERE user_id=? AND status='running'",
                                    (user_id,)).fetchone()
                if running:
                    raise RunBlocked("running", running["id"])
                last = c.execute(
                    "SELECT started_at FROM runs WHERE user_id=? AND status IN ('running','succeeded') "
                    "ORDER BY started_at DESC LIMIT 1", (user_id,)).fetchone()
                if last and min_interval_s > 0:
                    ready = datetime.fromisoformat(last["started_at"]) + timedelta(seconds=min_interval_s)
                    wait = (ready - datetime.now(timezone.utc)).total_seconds()
                    if wait > 0:
                        raise RunBlocked("rate_limited", retry_after=int(wait) + 1)
                busy = c.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]
                if busy >= max_concurrent:
                    raise RunBlocked("busy", retry_after=60)
                run_id = uuid.uuid4().hex
                c.execute("INSERT INTO runs (id, user_id, status, stage, message, options, started_at) "
                          "VALUES (?, ?, 'running', 'starting', '', ?, ?)",
                          (run_id, user_id, json.dumps(options), _now()))
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        return self.get_run(user_id, run_id)  # type: ignore[return-value]

    def update_run(self, run_id: str, stage: str, message: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE runs SET stage=?, message=? WHERE id=?", (stage, message, run_id))

    def finish_run(self, run_id: str, *, error: str | None = None,
                   counts: dict | None = None, warnings: list[str] | None = None) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE runs SET status=?, stage=?, message='', error=?, counts=?, warnings=?, "
                "finished_at=? WHERE id=?",
                ("failed" if error else "succeeded", "failed" if error else "done", error,
                 json.dumps(counts) if counts is not None else None,
                 json.dumps(warnings or []), _now(), run_id))

    @staticmethod
    def _run(row: sqlite3.Row) -> dict:
        return {
            "run_id": row["id"], "status": row["status"], "stage": row["stage"],
            "message": row["message"] or "", "error": row["error"],
            "counts": json.loads(row["counts"]) if row["counts"] else None,
            "warnings": json.loads(row["warnings"]) if row["warnings"] else [],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
        }

    def get_run(self, user_id: int, run_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
        return self._run(row) if row else None

    def latest_run(self, user_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM runs WHERE user_id=? ORDER BY started_at DESC LIMIT 1",
                            (user_id,)).fetchone()
        return self._run(row) if row else None

    # ------------------------------------------------------------------- jobs
    def seen_ids(self, user_id: int) -> set[str]:
        with self.connect() as c:
            return {r["job_id"] for r in
                    c.execute("SELECT job_id FROM jobs WHERE user_id=?", (user_id,))}

    def save_jobs(self, user_id: int, jobs: list[Job], run_id: str) -> int:
        now = _now()
        rows = [(user_id, j.job_id, j.ats, j.company, j.title, j.location, j.url, j.description,
                 j.posted_at, j.salary, j.score, j.reason,
                 json.dumps(j.draft, ensure_ascii=False) if j.draft else None, run_id, now)
                for j in jobs]
        with self.connect() as c:
            c.execute("BEGIN")
            c.executemany(
                "INSERT OR IGNORE INTO jobs (user_id, job_id, ats, company, title, location, url, "
                "description, posted_at, salary, score, reason, draft, run_id, first_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            c.execute("COMMIT")
        return len(rows)

    @staticmethod
    def _job(row: sqlite3.Row, detail: bool) -> dict:
        out: dict[str, Any] = {
            "job_id": row["job_id"], "company": row["company"], "title": row["title"],
            "location": row["location"], "url": row["url"], "posted_at": row["posted_at"],
            "salary": row["salary"], "score": row["score"], "reason": row["reason"],
            "status": row["status"], "applied_on": row["applied_on"],
            "first_seen": row["first_seen"], "has_draft": bool(row["draft"]),
        }
        if detail:
            out["description"] = row["description"] or ""
            out["draft"] = json.loads(row["draft"]) if row["draft"] else None
        return out

    @staticmethod
    def _filters(user_id: int, min_score: float, company: str | None, q: str | None,
                 location: str | None, status: str | None = None) -> tuple[str, list]:
        where, args = ["user_id = ?", "COALESCE(score, 0) >= ?"], [user_id, min_score]
        if company:
            where.append("LOWER(company) = LOWER(?)")
            args.append(company)
        if q:
            where.append("(LOWER(title) LIKE ? OR LOWER(company) LIKE ?)")
            args += [f"%{q.lower()}%"] * 2
        if location:
            where.append("LOWER(location) LIKE ?")
            args.append(f"%{location.lower()}%")
        if status:
            where.append("status = ?")
            args.append(status)
        return " AND ".join(where), args

    def list_jobs(self, user_id: int, *, min_score: float = 0, company: str | None = None,
                  q: str | None = None, location: str | None = None,
                  status: str | None = None, limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
        clause, args = self._filters(user_id, min_score, company, q, location, status)
        with self.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM jobs WHERE {clause}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT * FROM jobs WHERE {clause} "
                "ORDER BY score DESC, first_seen DESC, job_id LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
        return total, [self._job(r, detail=False) for r in rows]

    def postings_for_insights(self, user_id: int, *, min_score: float = 0,
                              company: str | None = None, q: str | None = None,
                              location: str | None = None, cap: int = 2000) -> list[dict]:
        """Title + description of the newest `cap` stored jobs matching the filters."""
        clause, args = self._filters(user_id, min_score, company, q, location)
        with self.connect() as c:
            rows = c.execute(
                f"SELECT title, description FROM jobs WHERE {clause} "
                "ORDER BY first_seen DESC, job_id LIMIT ?", [*args, cap]).fetchall()
        return [{"title": r["title"] or "", "description": r["description"] or ""} for r in rows]

    def get_job(self, user_id: int, job_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE user_id=? AND job_id=?",
                            (user_id, job_id)).fetchone()
        return self._job(row, detail=True) if row else None

    def mark_applied(self, user_id: int, job_id: str) -> bool:
        with self.connect() as c:
            cur = c.execute("UPDATE jobs SET status='applied', applied_on=COALESCE(applied_on, ?) "
                            "WHERE user_id=? AND job_id=?", (_now(), user_id, job_id))
        return cur.rowcount > 0

    def stats(self, user_id: int) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT COUNT(*) AS tracked, "
                            "COALESCE(SUM(status='applied'), 0) AS applied, "
                            "COALESCE(SUM(draft IS NOT NULL), 0) AS drafted "
                            "FROM jobs WHERE user_id=?", (user_id,)).fetchone()
        return {"tracked": row["tracked"], "applied": row["applied"], "drafted": row["drafted"]}
