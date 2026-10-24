"""Fetch jobs from public ATS APIs. No auth, no scraping, no ToS risk."""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable

import requests

UA = {"User-Agent": "jobhunt/1.0 (personal job search agent)"}
TIMEOUT = 20

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


@dataclass
class Job:
    job_id: str          # stable global id for dedupe: "<ats>:<slug>:<id>"
    ats: str
    company: str
    title: str
    location: str
    url: str
    description: str
    posted_at: str | None = None
    salary: str | None = None
    # filled in later by the pipeline
    score: float | None = None
    reason: str | None = None
    draft: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Adapters. Each takes the raw JSON body and returns list[Job].
# Keeping parse separate from HTTP is what makes offline testing possible.
# --------------------------------------------------------------------------

def parse_greenhouse(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    for j in (body or {}).get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        out.append(Job(
            job_id=f"greenhouse:{slug}:{j.get('id')}",
            ats="greenhouse",
            company=company,
            title=(j.get("title") or "").strip(),
            location=loc.strip(),
            url=j.get("absolute_url") or "",
            description=strip_html(j.get("content")),
            posted_at=j.get("updated_at") or j.get("first_published"),
        ))
    return out


def parse_lever(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    for j in (body or []):
        cats = j.get("categories") or {}
        # Lever splits the JD across descriptionPlain + a `lists` array.
        chunks = [j.get("descriptionPlain") or strip_html(j.get("description"))]
        for lst in (j.get("lists") or []):
            chunks.append(str(lst.get("text") or ""))
            chunks.append(strip_html(lst.get("content")))
        chunks.append(j.get("additionalPlain") or strip_html(j.get("additional")))
        ts = j.get("createdAt")
        posted = None
        if isinstance(ts, (int, float)):
            posted = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        out.append(Job(
            job_id=f"lever:{slug}:{j.get('id')}",
            ats="lever",
            company=company,
            title=(j.get("text") or "").strip(),
            location=(cats.get("location") or "").strip(),
            url=j.get("hostedUrl") or j.get("applyUrl") or "",
            description="\n\n".join(c for c in chunks if c).strip(),
            posted_at=posted,
            salary=cats.get("commitment"),
        ))
    return out


def parse_ashby(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    for j in (body or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        salary = None
        summary = comp.get("compensationTierSummary") or comp.get("summaryComponents")
        if isinstance(summary, str):
            salary = summary
        out.append(Job(
            job_id=f"ashby:{slug}:{j.get('id')}",
            ats="ashby",
            company=company,
            title=(j.get("title") or "").strip(),
            location=(j.get("location") or "").strip(),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            description=(j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")) or "").strip(),
            posted_at=j.get("publishedAt"),
            salary=salary,
        ))
    return out


_WD_URL = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?([^/?#]+)")
_WD_AGO = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)
_WD_MULTI = re.compile(r"^\d+\s+locations?$", re.I)
WD_PAGE = 20  # Workday rejects larger page sizes with HTTP 400
WD_DEFAULT_TERMS = ["software engineer", "backend engineer", "python", "ai engineer", "llm"]


def parse_workday_url(url: str) -> tuple[str, str, str]:
    """https://<tenant>.wd<N>.myworkdayjobs.com/[locale/]<site> -> (tenant, wdN, site)."""
    m = _WD_URL.match(url or "")
    if not m:
        raise ValueError(f"not a Workday careers URL: {url!r}")
    return m.group(1), m.group(2), m.group(3)


def parse_workday_posted(text: str | None, today: date | None = None) -> str | None:
    """'Posted 3 Days Ago' / 'Posted Today' / 'Posted 30+ Days Ago' -> ISO date."""
    if not text:
        return None
    today = today or date.today()
    low = text.lower()
    if "today" in low:
        return today.isoformat()
    if "yesterday" in low:
        return (today - timedelta(days=1)).isoformat()
    m = _WD_AGO.search(text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return None


def parse_workday_stub(tenant: str, company: str, base: str, post: dict) -> Job:
    """A search-result row: enough to pre-filter, but no description yet."""
    loc = (post.get("locationsText") or "").strip()
    req = (post.get("bulletFields") or [""])[0]
    path = post.get("externalPath") or ""
    return Job(
        job_id=f"workday:{tenant}:{req or path.rsplit('_', 1)[-1]}",
        ats="workday",
        company=company,
        title=(post.get("title") or "").strip(),
        # "3 Locations" says nothing useful; leave it for the detail stage to resolve.
        location="" if _WD_MULTI.match(loc) else loc,
        url=f"{base}{path}",
        description="",
        posted_at=parse_workday_posted(post.get("postedOn")),
    )


def apply_workday_detail(job: Job, detail: Any) -> Job:
    info = (detail or {}).get("jobPostingInfo") or {}
    locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    job.location = ", ".join(l.strip() for l in locs if l and l.strip()) or job.location
    job.description = strip_html(info.get("jobDescription"))
    job.url = info.get("externalUrl") or job.url
    job.posted_at = info.get("startDate") or job.posted_at
    return job


def fetch_workday(entry: dict, session: requests.Session | None = None,
                  keep: Callable[[Job], bool] | None = None, sleep: float = 0.25,
                  search_terms: list[str] | None = None, max_pages: int = 2) -> list[Job]:
    """One Workday careers site. Never raises.

    Search rows carry no description, so each surviving posting costs one extra GET.
    `keep` runs on the bare row first so filtered-out roles never cost that request.
    """
    sess = session or requests
    name = entry.get("name") or entry.get("url")
    try:
        tenant, cluster, site = parse_workday_url(entry["url"])
    except (KeyError, ValueError) as e:
        print(f"  ! workday/{name} -> {e}")
        return []
    host = f"https://{tenant}.{cluster}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}"
    base = f"{host}/{site}"
    terms = entry.get("search") or search_terms or WD_DEFAULT_TERMS

    stubs: dict[str, Job] = {}
    detail_paths: dict[str, str] = {}
    try:
        for term in terms:
            for page in range(max_pages):
                r = sess.post(f"{api}/jobs", headers={**UA, "Accept": "application/json"},
                              json={"appliedFacets": {}, "limit": WD_PAGE,
                                    "offset": page * WD_PAGE, "searchText": term},
                              timeout=TIMEOUT)
                if r.status_code != 200:
                    print(f"  ! workday/{tenant} -> HTTP {r.status_code}")
                    break
                posts = r.json().get("jobPostings") or []
                for p in posts:
                    path = p.get("externalPath")
                    if not path or path in detail_paths:
                        continue
                    stub = parse_workday_stub(tenant, name, base, p)
                    detail_paths[path] = stub.job_id
                    if keep is None or keep(stub):
                        stubs[path] = stub
                time.sleep(sleep)
                if len(posts) < WD_PAGE:
                    break

        jobs = []
        for path, stub in stubs.items():
            try:
                r = sess.get(f"{api}{path}", headers={**UA, "Accept": "application/json"},
                             timeout=TIMEOUT)
                if r.status_code == 200:
                    jobs.append(apply_workday_detail(stub, r.json()))
            except Exception as e:
                print(f"  ! workday/{tenant}{path} -> {type(e).__name__}: {e}")
            time.sleep(sleep)
        return jobs
    except Exception as e:
        print(f"  ! workday/{tenant} -> {type(e).__name__}: {e}")
        return []


ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", parse_greenhouse),
    "lever":      ("https://api.lever.co/v0/postings/{slug}?mode=json", parse_lever),
    "ashby":      ("https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", parse_ashby),
}


def fetch_board(ats: str, slug: str, company: str | None = None,
                session: requests.Session | None = None) -> list[Job]:
    """Hit one company's public board. Returns [] on any failure (never raises)."""
    if ats not in ENDPOINTS:
        raise ValueError(f"unknown ATS: {ats}")
    url_tpl, parser = ENDPOINTS[ats]
    sess = session or requests
    try:
        r = sess.get(url_tpl.format(slug=slug), headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ! {ats}/{slug} -> HTTP {r.status_code}")
            return []
        return parser(slug, company or slug, r.json())
    except Exception as e:  # dead slug, rate limit, network blip
        print(f"  ! {ats}/{slug} -> {type(e).__name__}: {e}")
        return []


def fetch_all(companies: Iterable[dict], sleep: float = 0.25,
              keep: Callable[[Job], bool] | None = None,
              workday: dict | None = None) -> list[Job]:
    """Fetch every configured board.

    `keep` is an optional cheap predicate used by the Workday adapter to skip the
    per-job detail request for postings the filters would drop anyway.
    """
    jobs: list[Job] = []
    session = requests.Session()
    for c in companies:
        if c.get("ats") == "workday":
            got = fetch_workday(c, session=session, keep=keep, sleep=sleep, **(workday or {}))
            label = c.get("name") or c.get("url")
        else:
            got = fetch_board(c["ats"], c["slug"], c.get("name"), session=session)
            label = c.get("name") or c["slug"]
        if got:
            print(f"  {label:<28} {len(got):>4} jobs  ({c['ats']})")
        jobs.extend(got)
        time.sleep(sleep)
    return jobs
