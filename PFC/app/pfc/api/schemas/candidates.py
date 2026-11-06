"""REQ-IN-23..25: masked candidate profiles and CT-only identity resolution."""

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from pfc.services.ingestion.workbooks import valid_ct_email

from .cv import PublicCVFacts
from .occ_approvals import CamelModel

IdentityStatus = Literal["complete", "incomplete", "ambiguous", "review_later"]
Classification = Literal["Internal", "External", "Unknown"]


class ClassificationOverride(CamelModel):
    classification: Classification | None
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A non-blank reason is required")
        return value.strip()


class CandidateClassification(CamelModel):
    candidate_id: int
    effective_classification: Classification
    computed_classification: Classification
    classification_override: Classification | None
    override_reason: str | None
    override_by: int | None
    override_at: str | None


class IdentityResolution(CamelModel):
    action: Literal["generate_internal_id", "select_ct_record", "review_later", "manual_entry"]
    resource_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_selection(self) -> "IdentityResolution":
        if (self.action == "select_ct_record") != (self.resource_id is not None):
            raise ValueError("Only select_ct_record requires a resourceId")
        if self.action == "manual_entry":
            if not self.name:
                raise ValueError("Manual entry requires a nonblank name")
            if self.email is not None and not valid_ct_email(self.email.strip().lower()):
                raise ValueError("Email syntax is invalid")
        elif self.name is not None or self.email is not None:
            raise ValueError("Only manual_entry accepts a name or email")
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
    classification: CandidateClassification
    internal_ref: str
    resource: CandidateResource
    phone_masked: str | None
    emails: list[str]
    identity_status: IdentityStatus
    ct_email_options: list[CTEmailOption]
    cv_versions: list[CandidateCV]
    matches: list[dict[str, Any]]
    activity: list[CandidateActivity]
