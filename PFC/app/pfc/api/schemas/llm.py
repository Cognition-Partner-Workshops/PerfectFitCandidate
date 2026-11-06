from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool | None = None
    is_default: Literal[True] | None = Field(default=None, alias="isDefault")

    @model_validator(mode="after")
    def validate_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide approved or isDefault")
        if "approved" in self.model_fields_set and self.approved is None:
            raise ValueError("approved must be a boolean")
        if "is_default" in self.model_fields_set and self.is_default is None:
            raise ValueError("isDefault must be true")
        return self


class ActiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: int | None = Field(ge=1)


class LlmHealth(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    detail: str
    model: str | None = None
