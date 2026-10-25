"""fetch -> prefilter -> screen -> draft as a plain function.

The CLI prints its way through the same stages; the API needs them as a callable
that reports progress and raises instead of exiting. No I/O besides the network
calls the stages themselves make.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import llm
from .fetch import Job, fetch_all
from .prefilter import prefilter, reject_reason
from .providers import LLMError, resolve

Progress = Callable[[str, str], None]


class PipelineError(RuntimeError):
    """A run that cannot produce anything useful (bad config, no key, all batches failed)."""


@dataclass
class PipelineResult:
    scanned: int = 0
    passed_filters: int = 0
    candidates: int = 0
    jobs: list[Job] = field(default_factory=list)       # every job that got a score
    shortlist: list[Job] = field(default_factory=list)  # cleared the threshold, drafted
    warnings: list[str] = field(default_factory=list)


def run_pipeline(profile: dict, cfg: dict, seen_ids: set[str], companies: list[dict], *,
                 draft: bool = True, limit: int | None = None,
                 progress: Progress | None = None) -> PipelineResult:
    note = progress or (lambda stage, message: None)
    filters = cfg.get("filters") or {}
    result = PipelineResult()

    if not companies:
        raise PipelineError("no companies configured")

    # Resolve the screening model before the slow fetch: a missing key should
    # fail in a second, not after minutes of network calls.
    try:
        screen_provider, screen_model = resolve("screen")
    except LLMError as e:
        raise PipelineError(f"screening model unavailable: {e}") from e

    def keep(job: Job) -> bool:
        return job.job_id not in seen_ids and \
            reject_reason(job, filters, location_unknown_ok=True) is None

    fetched: dict[str, Job] = {}
    for i, company in enumerate(companies, 1):
        name = company.get("name") or company.get("slug") or company.get("url")
        note("fetching", f"{i}/{len(companies)} {name}")
        for job in fetch_all([company], keep=keep, workday=cfg.get("workday") or {}):
            fetched.setdefault(job.job_id, job)
    result.scanned = len(fetched)
    if not fetched:
        raise PipelineError("no postings fetched; check companies.yaml")

    note("filtering", f"{len(fetched)} postings")
    passed = prefilter(list(fetched.values()), filters)
    result.passed_filters = len(passed)
    jobs = [j for j in passed if j.job_id not in seen_ids]
    result.candidates = len(jobs)

    if limit and len(jobs) > limit:
        # Cost guard: keep the newest, leave the rest unrecorded for the next run.
        jobs.sort(key=lambda j: j.posted_at or "", reverse=True)
        jobs = jobs[:limit]
    if not jobs:
        return result

    note("screening", f"{len(jobs)} jobs")
    llm.screen(jobs, profile,
               batch_size=int(cfg.get("screen_batch_size", 8)),
               jd_chars=int(cfg.get("screen_jd_chars", 1400)),
               provider=screen_provider, model=screen_model)
    scored = [j for j in jobs if j.score is not None]
    if not scored:
        raise PipelineError("screening scored nothing: every batch failed "
                            "(bad key, rate limit or wrong model id)")
    if len(scored) < len(jobs):
        result.warnings.append(f"{len(jobs) - len(scored)} jobs were not scored and will retry next run")
    result.jobs = scored

    threshold = float(cfg.get("score_threshold", 7.0))
    top_n = int(cfg.get("max_per_digest", 5))
    result.shortlist = sorted((j for j in scored if j.score >= threshold),
                              key=lambda j: j.score, reverse=True)[:top_n]

    if draft and result.shortlist:
        note("drafting", f"{len(result.shortlist)} jobs")
        try:
            provider, model = resolve("draft")
            llm.draft(result.shortlist, profile,
                      jd_chars=int(cfg.get("draft_jd_chars", 6000)),
                      provider=provider, model=model)
        except LLMError as e:
            result.warnings.append(f"drafting unavailable: {e}")
    return result
