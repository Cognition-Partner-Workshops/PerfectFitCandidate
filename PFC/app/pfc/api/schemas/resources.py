"""REQ-OUT-03 / REQ-ELIG-08: masked resource browsing contracts."""

from pydantic import BaseModel, Field


class ResourceQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)
    grade: str | None = None
    bench_status_code: str | None = None
    eligibility: str | None = None
    lifecycle: str = "active"
    group_name: str | None = None
    location: str | None = None
    q: str | None = None


class ResourceItem(BaseModel):
    resource_id: int
    candidate_id: int | None
    cv_versions_count: int
    short_name: str
    email_address: str | None
    grade: str | None
    location: str | None
    bench_status: str | None
    bench_status_code: str | None
    skills_raw: str | None
    available_capacity: str | None
    bench_start_date: str | None
    bench_end_date: str | None
    group_name: str | None
    lifecycle: str
    eligibility: str
    exclusion_reasons: list[str]
    pending_review: bool
    unmapped_status: bool
    occ_pending_review: bool


class CVSummary(BaseModel):
    cv_version_id: int
    version_no: int
    extracted_at: str


class ResourceDetail(ResourceItem):
    latest_cv: CVSummary | None


class ResourcePage(BaseModel):
    items: list[ResourceItem]
    page: int
    page_size: int
    total: int
