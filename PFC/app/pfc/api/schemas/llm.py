from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class ActiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: int = Field(ge=1)


class LlmHealth(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    detail: str
    model: str | None = None
