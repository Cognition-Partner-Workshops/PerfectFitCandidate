"""Dashboard wire contracts and dataset-specific filters."""

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from pfc.api.schemas.match import WireModel
from pfc.api.schemas.results import Band, ResultsQuery, band_label


class ExportQuery(ResultsQuery):
    format: Literal["csv", "xlsx"] = "csv"
    include_emails: bool = False


class DashboardFilters(WireModel):
    code_requisition: list[str] | None = None
    account_name: list[str] | None = None
    bu: list[str] | None = None
    grade: list[str] | None = None
    location: list[str] | None = None
    region: list[str] | None = None
    band: list[Band | Literal[""]] | None = None
    status: list[str] | None = None
    candidate_type: list[Literal["Internal", "External", "Unknown", ""]] | None = None
    incomplete_identity: list[Literal["yes", "no", ""]] | None = None
    mandatory_skills: list[Literal["met", "gap", ""]] | None = None
    score_min: float | None = Field(default=None, ge=0, le=100)
    score_max: float | None = Field(default=None, ge=0, le=100)
    run_id: int | None = Field(default=None, gt=0)

    @field_validator("band", mode="before")
    @classmethod
    def normalize_band(cls, value: str | list[str] | None) -> list[str] | None:
        if value is None:
            return None
        values = value if isinstance(value, list) else [value]
        return [band_label(item) for item in values]

    @model_validator(mode="after")
    def score_range(self) -> Self:
        if (
            self.score_min is not None
            and self.score_max is not None
            and self.score_min > self.score_max
        ):
            raise ValueError("scoreMin exceeds scoreMax")
        return self


class CountItem(WireModel):
    label: str
    count: int


class MatchRun(WireModel):
    run_id: int
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    finished_at: str | None
    llm_model: str
    template_version: int
    demands_assessed: int
    pairs_scored: int
    roles_without_candidate: int


class Demand(WireModel):
    demand_id: int
    code_requisition: str
    id_format_ok: bool
    position_start_date: str | None
    account_name: str | None
    code_local_grade: str | None
    demand_request_name: str | None
    region: str | None
    bu: str | None
    owner: str | None
    jd_text: str
    jd_status: Literal[
        "Not analysed",
        "Analysed — awaiting confirmation",
        "Confirmed — scoring",
        "Scored",
        "JD changed",
    ]
    matches: int
    best_band: Band | None
    uploaded_at: str
    mandatory_skills: list[str]
    preferred_skills: list[str]


class Resource(WireModel):
    resource_id: int
    candidate_id: int
    short_name: str
    email_address_masked: str | None
    name: str | None
    grade: str | None
    location: str | None
    bench_status: str | None
    bench_status_priority: int | None
    unmapped_status: bool
    skills: list[str]
    available_capacity: Literal["Y", "N"] | None
    bench_start_date: str | None
    bench_end_date: str | None
    group_name: str | None
    group_owner: str | None
    eligibility: Literal["eligible", "eligible_caveat", "excluded", "insufficient"]
    exclusion_reasons: list[str]
    cv_status: Literal["Synced", "Not found", "Partial", "Error", "Not synced"]
    cv_count: int
    candidate_type: Literal["Internal", "External", "Unknown"]
    identity_status: Literal["complete", "incomplete", "ambiguous", "review_later"]


class DataQuality(WireModel):
    total: int
    dl: int
    ct: int


class AnalyticsSummary(WireModel):
    run: MatchRun | None
    runs_included: int
    requisitions_assessed: int
    open_requisitions: int
    role_to_person_combinations: int
    roles_with_no_suitable_candidate: int
    weak_only_requisitions: int
    data_quality_issues: DataQuality
    eligible_bench: int
    excluded_bench: int
    match_coverage_pct: float
    emails_within24h_pct: float = Field(alias="emailsWithin24hPct")


class BuRegion(WireModel):
    bu: str
    region: str
    count: int


class DemandAnalytics(WireModel):
    open_by_bu_region: list[BuRegion]
    top_skills: list[CountItem]
    grade_to_jd_count: list[CountItem]
    top_accounts: list[CountItem]
    start_pipeline: list[CountItem]
    rows: list[Demand]


class DemandSupply(WireModel):
    skill: str
    demand_count: int
    supply_y: int
    supply_n: int


class GradeHistogram(WireModel):
    grade: str
    bench: int
    demand: int


class Timeline(WireModel):
    name: str
    start: str
    end: str
    days: int


class StatusCount(WireModel):
    status: str
    count: int
    excluded: bool


class BenchAnalytics(WireModel):
    top_skills: list[CountItem]
    demand_vs_supply: list[DemandSupply]
    grade_histogram: list[GradeHistogram]
    timeline: list[Timeline]
    aging: list[CountItem]
    status_distribution: list[StatusCount]
    rows: list[Resource]


class ValidationCheck(WireModel):
    id: str
    label: str
    passed: bool
    detail: str


class MatchAnalytics(WireModel):
    coverage_pct: float
    band_distribution: list[CountItem]
    email_status_counts: list[CountItem]
    validation: list[ValidationCheck]
