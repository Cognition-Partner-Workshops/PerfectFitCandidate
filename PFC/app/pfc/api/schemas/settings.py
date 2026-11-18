from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pfc.cv_formats import ALLOWED_CV_EXTENSIONS, DEFAULT_CV_EXTENSIONS


class WatchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    poll_minutes: int = Field(default=5, ge=1, le=1440)
    require_preview: bool = False
    path: str | None = Field(default=None, max_length=4096)
    pattern: str = Field(default="DL_*.xlsx", min_length=1, max_length=255)

    @field_validator("path", "pattern", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class WatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dl: WatchSource = Field(default_factory=WatchSource)
    ct: WatchSource = Field(default_factory=lambda: WatchSource(pattern="CT_*.xlsx"))


class PersonalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    resume_root_path: str | None = Field(default=None, max_length=4096)
    resume_file_types: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CV_EXTENSIONS), alias="resumeFileTypes"
    )
    watch: WatchSettings = Field(default_factory=WatchSettings)
    active_model_id: int | None = Field(default=None, ge=1)
    density: Literal["comfortable", "compact"] = "comfortable"
    page_size: int = Field(default=25, ge=1, le=1000)
    default_landing: str = Field(default="dashboard", min_length=1, max_length=100)

    @field_validator("resume_file_types", mode="before")
    @classmethod
    def file_types(cls, value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("Select at least one CV file format")
        result = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("Invalid CV file format")
            extension = "." + item.strip().lower().lstrip(".")
            if extension not in ALLOWED_CV_EXTENSIONS:
                raise ValueError("Unsupported CV file format")
            if extension not in result:
                result.append(extension)
        return result

    @field_validator("resume_root_path", "default_landing", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class PersonalSettingsView(PersonalSettings):
    resume_converter_available: bool = Field(json_schema_extra={"readOnly": True})
