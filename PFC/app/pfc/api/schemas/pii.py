from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UnmaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: Literal["resource", "candidate", "occ_user"]
    id: int = Field(ge=1)
    field: Literal["email", "phone"]


class UnmaskResponse(BaseModel):
    value: str
