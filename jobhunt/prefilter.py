"""Deterministic filter that runs BEFORE any LLM call.

This is the whole cost story: ~2000 raw jobs -> ~40 candidates for ~0 rupees,
so Claude only ever reads jobs that already passed title + location + freshness.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .fetch import Job

REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed")


def _any_match(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def reject_reason(job: Job, cfg: dict, location_unknown_ok: bool = False) -> str | None:
    """Why a job fails the filters ("title" | "location" | "age"), or None if it passes.

    `location_unknown_ok` lets a partially-fetched job (empty location) through the
    location gate; it is re-checked once the full posting is loaded.
    """
    inc = cfg.get("include_titles") or [r"."]
    exc = cfg.get("exclude_titles") or []
    locs = [l.lower() for l in (cfg.get("locations") or [])]
    allow_remote = bool(cfg.get("allow_remote", True))
    max_age = cfg.get("max_age_days")

    if not _any_match(inc, job.title) or (exc and _any_match(exc, job.title)):
        return "title"

    if locs and not (location_unknown_ok and not job.location):
        hay = f"{job.location} {job.title}".lower()
        is_remote = allow_remote and any(h in hay for h in REMOTE_HINTS)
        if not is_remote and not any(l in hay for l in locs):
            return "location"

    if max_age:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
        posted = _parse_date(job.posted_at)
        if posted and posted < cutoff:
            return "age"
    return None


def prefilter(jobs: list[Job], cfg: dict) -> list[Job]:
    kept, stats = [], {"title": 0, "location": 0, "age": 0}
    for j in jobs:
        reason = reject_reason(j, cfg)
        if reason:
            stats[reason] += 1
            continue
        kept.append(j)

    print(f"prefilter: {len(jobs)} -> {len(kept)} "
          f"(dropped title={stats['title']} location={stats['location']} stale={stats['age']})")
    return kept
