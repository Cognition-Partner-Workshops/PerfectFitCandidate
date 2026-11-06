"""Reviewed requirements and run contracts (REQ-MAT-02/03/08)."""

from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pfc.services.matching.requirements import Requirements


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["full", "demands"] = "full"
    demand_ids: list[int] = Field(default_factory=list)
    edited_requirements: dict[int, Requirements | None] = Field(default_factory=dict)
    as_of_date: date | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if any(value <= 0 for value in (*self.demand_ids, *self.edited_requirements)):
            raise ValueError("Demand IDs must be positive")
        if len(set(self.demand_ids)) != len(self.demand_ids):
            raise ValueError("Demand IDs must be unique")
        if self.scope == "demands":
            if not self.demand_ids:
                raise ValueError("Demand scope needs demand IDs")
            if not set(self.edited_requirements) <= set(self.demand_ids):
                raise ValueError("Confirmed demands must belong to the requested scope")
        elif self.demand_ids:
            raise ValueError("Full scope does not accept demand IDs")
        return self


class RequirementItemView(BaseModel):
    item_id: int
    jd_req_id: int
    kind: Literal["mandatory_skill", "preferred_skill", "experience", "qualification", "domain"]
    text: str
    canonical_skill_id: int | None
    source_span: str | None
    position: int


class RequirementsView(BaseModel):
    jd_req_id: int
    demand_version_id: int
    revision: int
    status: Literal["draft", "confirmed", "superseded"]
    extracted_by: str | None
    prompt_version: str | None
    extraction: Requirements
    location_norm: str | None
    onsite_required: bool | None
    min_years_exp: float | None
    confirmed_by: int | None
    confirmed_at: str | None
    created_at: str
    items: list[RequirementItemView]


class RunAccepted(BaseModel):
    run_id: int
    job_id: int


class RunView(BaseModel):
    run_id: int
    scope: Literal["full", "demands", "resources", "incremental"]
    scope_json: str | None
    template_id: int
    embedding_model_id: int
    llm_model: str | None
    engine_version: str
    synonyms_version: str
    config_hash: str
    triggered_by: int | None
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    started_at: str | None
    finished_at: str | None
    demands_assessed: int | None
    pairs_scored: int | None
    roles_without_candidate: int | None
    dq_summary_json: str | None


def extraction_payload(value: Requirements | None) -> dict[str, Any] | None:
    return value.model_dump(mode="json") if value is not None else None
