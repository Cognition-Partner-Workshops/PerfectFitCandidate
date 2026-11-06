from typing import Literal

from pydantic import BaseModel, Field, model_validator

EmailStatus = Literal["Draft", "Approved", "Sending", "Sent", "Failed", "Discarded"]


class EmailDraftCreate(BaseModel):
    run_id: int | None = Field(default=None, ge=1)
    demand_id: int | None = Field(default=None, ge=1)
    result_ids: list[int] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "EmailDraftCreate":
        if (self.run_id is None) == (self.demand_id is None):
            raise ValueError("Provide exactly one of run_id or demand_id")
        if self.result_ids is not None and (
            any(result_id < 1 for result_id in self.result_ids)
            or len(set(self.result_ids)) != len(self.result_ids)
        ):
            raise ValueError("result_ids must be unique positive integers")
        return self


class EmailDraftEdit(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    body_html: str | None = Field(default=None, min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def validate_fields(self) -> "EmailDraftEdit":
        if self.subject is None and self.body_html is None:
            raise ValueError("Provide a subject or body_html")
        return self


class EmailSendRequest(BaseModel):
    draft_ids: list[int] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> "EmailSendRequest":
        if self.draft_ids is not None and (
            any(message_id < 1 for message_id in self.draft_ids)
            or len(set(self.draft_ids)) != len(self.draft_ids)
        ):
            raise ValueError("draft_ids must be unique positive integers")
        return self


class EmailDraftView(BaseModel):
    message_id: int
    demand_id: int
    resource_id: int
    result_id: int | None
    run_id: int | None
    rank: int | None
    selected: bool
    to_address: str
    cc: list[str]
    subject: str
    body_html: str
    status: EmailStatus
    drafted_by_model: str | None
    created_at: str
    updated_at: str
    sent_at: str | None
    last_error: str | None
    attachment_name: str | None


class EmailDraftIssueView(BaseModel):
    result_id: int
    resource_id: int
    code: str
    detail: str


class EmailDraftCreateResponse(BaseModel):
    drafts: list[EmailDraftView]
    issues: list[EmailDraftIssueView]


class EmailDraftListResponse(BaseModel):
    drafts: list[EmailDraftView]
    status_counts: dict[str, int]
    sent_count: int


class EmailSendOutcomeView(BaseModel):
    message_id: int
    status: Literal["sent", "failed", "skipped"]
    detail: str | None = None


class EmailSendResponse(BaseModel):
    outcomes: list[EmailSendOutcomeView]
    sent_count: int
    failed_count: int
    skipped_count: int
