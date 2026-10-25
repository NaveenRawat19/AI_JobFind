"""Request/response models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    # 128 cap: scrypt cost grows with input, so an unbounded password is a DoS lever.
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    created_at: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    idle_timeout_seconds: int
    expires_at: str = Field(description="Hard session limit; the idle timeout usually ends it sooner.")
    has_profile: bool = Field(description="False until a resume has been uploaded (POST /resume).")


class MeOut(UserOut):
    idle_timeout_seconds: int
    has_profile: bool


class Profile(BaseModel):
    """Same shape as profile.json. Unknown keys from the LLM are dropped."""
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    current_title: str = ""
    years_experience: float = Field(0, ge=0, le=60)
    core_skills: list[str] = []
    domains: list[str] = []
    notable_projects: list[str] = []
    education: str = ""
    target_titles: list[str] = []
    seniority: str = ""


class ProfileUpdate(BaseModel):
    """PUT /profile: only the fields you send are changed."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    current_title: str | None = None
    years_experience: float | None = Field(None, ge=0, le=60)
    core_skills: list[str] | None = None
    domains: list[str] | None = None
    notable_projects: list[str] | None = None
    education: str | None = None
    target_titles: list[str] | None = None
    seniority: str | None = None


class ResumeInfo(BaseModel):
    id: int = Field(description="Use with GET/DELETE /resumes/{id}.")
    filename: str
    media_type: str
    size: int = Field(description="Bytes.")
    sha256: str
    uploaded_at: str


class ResumeListItem(ResumeInfo):
    is_current: bool = Field(description="The newest upload, the one GET /resume returns and reextract uses.")


class ProfileOut(BaseModel):
    profile: Profile
    source_file: str | None
    updated_at: str
    resume: ResumeInfo | None = Field(
        None, description="The newest uploaded resume (download with GET /resume); null if none is stored.")


class SkillStat(BaseModel):
    skill: str
    category: str
    jobs: int = Field(description="Postings in the sample that mention it.")
    percent: float = Field(description="jobs / sample size, 0-100.")
    in_profile: bool | None = Field(None, description="Whether your profile lists it; null with no profile.")


class RoleCount(BaseModel):
    role: str
    jobs: int


class SkillInsights(BaseModel):
    sample_size: int = Field(description="Stored postings analysed. Percentages are out of this.")
    with_description: int = Field(description="Of those, how many had any description text.")
    truncated: bool = Field(description="True if more postings matched than were analysed.")
    note: str | None = None
    roles: list[RoleCount] = Field(description="Role families in the filtered sample, before `role` is applied.")
    skills: list[SkillStat]
    gaps: list[str] = Field(description="Top skills in demand that your profile does not list.")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: bool = Field(True, description="Also write application kits for the top matches.")
    limit: int | None = Field(None, ge=1, le=300,
                              description="Max new jobs to send to the LLM (cost guard).")
    companies: list[str] | None = Field(None, description="Only these companies, by name.")


class RunOut(BaseModel):
    run_id: str
    status: Literal["running", "succeeded", "failed"]
    stage: str | None
    message: str
    error: str | None
    counts: dict | None
    warnings: list[str]
    started_at: str
    finished_at: str | None


class JobOut(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    url: str
    posted_at: str | None
    salary: str | None
    score: float | None
    reason: str | None
    status: Literal["new", "applied"]
    applied_on: str | None
    first_seen: str
    has_draft: bool


class DraftKit(BaseModel):
    fit_summary: str = ""
    tailored_bullets: list[str] = []
    gaps: list[str] = []
    cover_note: str = ""
    questions_to_ask: list[str] = []


class JobDetail(JobOut):
    description: str
    draft: DraftKit | None


class JobList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]
