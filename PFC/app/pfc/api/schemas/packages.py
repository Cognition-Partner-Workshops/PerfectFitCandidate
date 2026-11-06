"""Signed package HTTP contracts (REQ-OCC-01..03)."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from pfc.security.package_signing import Manifest
from pfc.services.packages.models import ModelMetadata


class PackageSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="forbid"
    )


class ExportRequest(PackageSchema):
    passphrase: str | None = Field(default=None, min_length=1, max_length=1024, repr=False)
    min_app_version: str | None = Field(default=None, max_length=100)
    max_app_version: str | None = Field(default=None, max_length=100)


class OneTimePassword(PackageSchema):
    email: str
    otp: str = Field(repr=False)


class CountItem(PackageSchema):
    label: str
    count: int


class PackageInfo(PackageSchema):
    file_name: str
    version: int
    created_at: str
    signed: bool = True
    contents: list[CountItem]


class ExportResponse(PackageInfo):
    content_base64: str
    one_time_passwords: list[OneTimePassword] = Field(repr=False)


class SigningKeyResponse(PackageSchema):
    key_id: str
    public_key_base64: str


class PackageChange(PackageSchema):
    area: str
    change: Literal["added", "changed", "removed"]
    item: str


class SettingsPreviewResponse(PackageSchema):
    preview_id: str
    file_name: str
    package_version: int
    installed_version: int
    signature_valid: bool = True
    schema_compatible: bool = True
    app_compatible: bool = True
    newer: bool = True
    changes: list[PackageChange]
    imported_accounts: int
    errors: list[str] = Field(default_factory=list)


class ModelPreviewResponse(PackageSchema):
    preview_id: str
    package_version: int
    manifest: Manifest
    metadata: ModelMetadata


class ApplyRequest(PackageSchema):
    preview_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{43}$")
    pending_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.preview_id is None) == (self.pending_id is None):
            raise ValueError("Specify exactly one previewId or pendingId")
        return self


class ApplyResponse(PackageSchema):
    version: int


class ModelApplyResponse(ApplyResponse):
    model_id: int
    job_id: int


class ReembedResponse(ApplyResponse):
    job_id: int


class PendingResponse(PackageSchema):
    id: str
    file_name: str
    kind: Literal["settings", "model"]
    version: int | None
    valid: bool
    error: str | None


class HistoryResponse(PackageSchema):
    version: int
    imported_at: str
    imported_by: str
    file_name: str
    can_rollback: bool
    status: Literal["applied", "rolled_back"]
    is_current: bool
