"""REQ-IN-23..25: masked candidate profiles and CT-only identity resolution."""

from typing import Any, Literal

from pydantic import Field, model_validator

from .cv import PublicCVFacts
from .occ_approvals import CamelModel

IdentityStatus = Literal["complete", "incomplete", "ambiguous", "review_later"]


class IdentityResolution(CamelModel):
    action: Literal["generate_internal_id", "select_ct_record", "review_later"]
    resource_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_selection(self) -> "IdentityResolution":
        if (self.action == "select_ct_record") != (self.resource_id is not None):
            raise ValueError("Only select_ct_record requires a resourceId")
        return self


class CandidateResource(CamelModel):
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
    identity_status: IdentityStatus


class CandidateCV(CamelModel):
    cv_version_id: int
    file_name: str
    folder_path: str
    modified_at: str
    extracted_at: str
    is_best: bool
    facts: PublicCVFacts | None = None


class CTEmailOption(CamelModel):
    resource_id: int
    label: str
    email_masked: str


class CandidateActivity(CamelModel):
    at: str
    summary: str


class CandidateDetail(CamelModel):
    candidate_id: int
    internal_ref: str
    resource: CandidateResource
    phone_masked: str | None
    emails: list[str]
    identity_status: IdentityStatus
    ct_email_options: list[CTEmailOption]
    cv_versions: list[CandidateCV]
    matches: list[dict[str, Any]]
    activity: list[CandidateActivity]
