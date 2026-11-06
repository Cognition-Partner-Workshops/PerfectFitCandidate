"""Masked result and non-destructive override contracts (REQ-OUT-01/03, REQ-MAT-09)."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pfc.services.matching import ResultFilters

Band = Literal["Strong", "Potential", "Avg", "Weak", "Insufficient data"]
OverrideAction = Literal["pin", "unpin", "exclude", "include", "adjust_band"]


class ResultsQuery(ResultFilters):
    run_id: int | None = Field(default=None, gt=0)
    sort: Literal[
        "rank",
        "score",
        "requisition",
        "account",
        "demand_grade",
        "person_grade",
        "person_location",
        "potential_person",
        "status_priority",
        "band",
        "start_date",
    ] = "rank"
    descending: bool = False
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class OverrideReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A reason is required")
        return value


class OverrideRequest(OverrideReason):
    action: OverrideAction
    new_band: Band | None = None

    @model_validator(mode="after")
    def band_required(self) -> Self:
        if (self.action == "adjust_band") != (self.new_band is not None):
            raise ValueError("new_band is required only for adjust_band")
        return self


class OverrideView(BaseModel):
    override_id: int
    demand_id: int
    resource_id: int
    result_id: int | None
    action: OverrideAction
    new_band: Band | None
    reason: str
    created_by: int
    created_at: str
    revoked_by: int | None
    revoked_at: str | None


class ResultRow(BaseModel):
    result_id: int
    run_id: int
    demand_id: int
    demand_version_id: int
    jd_req_id: int
    resource_id: int
    candidate_id: int | None
    best_cv_version_id: int | None
    requisition: str
    start_date: str | None
    account: str | None
    role: str | None
    demand_grade: str | None
    role_location: str
    potential_person: str
    person_email: str | None = Field(description="Masked source email; never an unmasked value")
    person_grade: str | None
    person_location: str | None
    score: float
    band: Band
    location_assessment: str
    matching_evidence: str
    gaps_checks: str
    eligibility_evidence: str
    rank: int
    shortlisted: bool
    email_preselected: bool
    grade_relation: Literal["exact", "adjacent_above", "adjacent_below"]
    status_priority: int | None
    mandatory_missing: int
    status: str | None
    overridden: bool
    effective_band: Band
    effective_shortlisted: bool
    effective_excluded: bool
    effective_email_preselected: bool
    display_band: str


class ResultsPage(BaseModel):
    run_id: int | None
    items: list[ResultRow]
    total: int
    limit: int
    offset: int


class BreakdownView(BaseModel):
    component: Literal["skills", "grade", "location", "bench_start", "bench_end", "status"]
    points: float
    max_points: float
    inputs: dict[str, Any]


class CvScoreView(BaseModel):
    cv_version_id: int | None
    score: float
    band: Band
    version_no: int | None
    is_latest: bool | None


class ResultDetail(ResultRow):
    breakdown: list[BreakdownView]
    cv_scores: list[CvScoreView]
    overrides: list[OverrideView]


class ExcludedRow(BaseModel):
    run_id: int
    resource_id: int
    demand_id: int | None
    reason_code: str
    detail: str | None
    short_name: str
    requisition: str | None


class DemandMatches(BaseModel):
    run_id: int | None
    items: list[ResultRow]
    no_suitable_candidate: bool
    weak_only: bool
    flags: list[str]
