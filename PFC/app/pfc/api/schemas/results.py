"""Frontend result contracts; label normalization stays at the HTTP boundary."""

from typing import Any, Literal, Self, cast

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_snake

from .match import WireModel

Band = Literal["Strong", "Potential", "Avg", "Weak", "Insufficient data"]
Action = Literal["pin", "unpin", "exclude", "include", "adjust_band"]


def band_label(value: str) -> Band:
    return cast(Band, {"Avg.": "Avg", "Weak Match": "Weak"}.get(value, value))


class ResultsQuery(WireModel):
    requisition: str | None = None
    account: str | None = None
    demand_grade: str | None = None
    person_grade: str | None = None
    person_location: str | None = None
    band: Band | None = None
    location_assessment: str | None = None
    mandatory_skill: str | None = None
    score_min: float | None = Field(default=None, ge=0, le=100)
    score_max: float | None = Field(default=None, ge=0, le=100)
    status: str | None = None
    shortlisted_only: bool = False
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
    limit: int | None = Field(default=None, ge=1, le=5000)
    offset: int = Field(default=0, ge=0)

    @field_validator("sort", mode="before")
    @classmethod
    def normalize_sort(cls, value: str) -> str:
        return to_snake(value)

    @field_validator("band", mode="before")
    @classmethod
    def normalize_band(cls, value: str | None) -> str | None:
        return band_label(value) if value is not None else None

    @model_validator(mode="after")
    def limits(self) -> Self:
        if (
            self.score_min is not None
            and self.score_max is not None
            and self.score_min > self.score_max
        ):
            raise ValueError("scoreMin exceeds scoreMax")
        return self


class OverrideReason(WireModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A reason is required")
        return value


class OverrideRequest(OverrideReason):
    action: Action
    new_band: Band | None = None

    @field_validator("new_band", mode="before")
    @classmethod
    def normalize_band(cls, value: str | None) -> str | None:
        return band_label(value) if value is not None else None

    @model_validator(mode="after")
    def band_required(self) -> Self:
        if (self.action == "adjust_band") != (self.new_band is not None):
            raise ValueError("newBand is required only for adjust_band")
        return self


class OverrideView(WireModel):
    action: Action
    new_band: Band | None
    reason: str
    created_by: str
    created_at: str
    override_id: int
    revoked_at: str | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        output: dict[str, Any] = handler(self)
        if self.new_band is None:
            output.pop("newBand", None)
            output.pop("new_band", None)
        return output


class BreakdownView(WireModel):
    component: Literal["skills", "grade", "location", "bench_start", "bench_end", "status"]
    points: float
    max_points: float
    inputs: dict[str, Any] | None = None


class ResultRow(WireModel):
    result_id: int
    run_id: int
    demand_id: int
    resource_id: int
    candidate_id: int
    rank: int
    code_requisition: str
    position_start_date: str | None
    account_name: str | None
    bu: str | None
    demand_request_name: str | None
    demand_grade: str | None
    region: str | None
    person_name: str | None
    person_email_masked: str | None
    person_grade: str | None
    person_location: str | None
    bench_status: str | None
    score: float
    band: Band
    grade_relation: Literal["exact", "adjacent_above", "adjacent_below"]
    location_assessment: Literal[
        "Aligned", "Remote/unspecified", "Potential travel", "Location check"
    ]
    matching_evidence: list[str]
    gaps_checks: list[str]
    eligibility_evidence: list[str]
    mandatory_missing: int
    candidate_type: Literal["Internal", "External", "Unknown"]
    incomplete_identity: bool
    email_preselected: bool
    breakdown: list[BreakdownView]
    override: OverrideView | None
    effective_band: Band
    effective_shortlisted: bool
    shortlisted: bool

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        output: dict[str, Any] = handler(self)
        if self.override is None:
            output.pop("override", None)
        return output


class CvScoreView(WireModel):
    cv_version_id: int | None
    score: float
    band: Band
    version_no: int | None
    is_latest: bool | None


class ResultDetail(ResultRow):
    cv_scores: list[CvScoreView]
    overrides: list[OverrideView]


class ExcludedResource(WireModel):
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
    eligibility: Literal["excluded"] = "excluded"
    exclusion_reasons: list[str]
    cv_status: Literal["Synced", "Not found", "Partial", "Error", "Not synced"]
    cv_count: int
    candidate_type: Literal["Internal", "External", "Unknown"]
    identity_status: Literal["complete", "incomplete", "ambiguous", "review_later"]
