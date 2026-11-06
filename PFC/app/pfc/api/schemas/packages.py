"""Signed package HTTP contracts (REQ-OCC-01..03)."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pfc.security.package_signing import Manifest
from pfc.services.packages.models import ModelMetadata


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: str = Field(min_length=1, max_length=1024, repr=False)
    min_app_version: str | None = Field(default=None, max_length=100)
    max_app_version: str | None = Field(default=None, max_length=100)


class OneTimePassword(BaseModel):
    email: str
    otp: str = Field(repr=False)


class ExportResponse(BaseModel):
    file_name: str
    version: int
    content_base64: str
    one_time_passwords: list[OneTimePassword] = Field(repr=False)


class SigningKeyResponse(BaseModel):
    key_id: str
    public_key_base64: str


class PayloadDiffResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]


class SettingsPreviewResponse(BaseModel):
    token: str
    version: int
    manifest: Manifest
    diff: dict[str, PayloadDiffResponse]


class ModelPreviewResponse(BaseModel):
    token: str
    version: int
    manifest: Manifest
    metadata: ModelMetadata


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{43}$")
    pending_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.token is None) == (self.pending_id is None):
            raise ValueError("Specify exactly one preview token or pending inbox id")
        return self


class ApplyResponse(BaseModel):
    version: int


class ModelApplyResponse(ApplyResponse):
    model_id: int
    job_id: int


class PendingResponse(BaseModel):
    id: str
    file_name: str
    kind: Literal["settings", "model"]
    version: int | None
    valid: bool
    error: str | None


class HistoryResponse(BaseModel):
    version: int
    applied_at: str
    applied_by: str
    status: Literal["applied", "rolled_back"]
    is_current: bool
