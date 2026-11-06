"""REQ-ROLE-03: frontend OCC approval contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ApprovalField(CamelModel):
    field: str
    before: str | None
    after: str | None
    admin_value: str | None = None


class ApprovalEntry(CamelModel):
    entry_id: int
    source: Literal["DL", "CT"]
    target_id: int
    key: str
    change: Literal["New", "Updated", "Removed"]
    uploaded_by: str
    uploaded_at: str
    state: Literal["Pending", "Approved", "Modified", "Reverted"]
    conflict: bool
    fields: list[ApprovalField]
    comment: str | None = None


class ApprovalDecision(CamelModel):
    action: Literal["approve", "modify", "revert"]
    comment: str | None = Field(default=None, max_length=2000)
    values: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_decision(self) -> "ApprovalDecision":
        if self.action == "modify":
            if not self.comment or not self.comment.strip() or not self.values:
                raise ValueError("Modifications require a comment and values")
        elif self.values:
            raise ValueError("Only modifications accept values")
        return self
