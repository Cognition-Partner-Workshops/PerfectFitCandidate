from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WatchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
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
    model_config = ConfigDict(extra="forbid")
    resume_root_path: str | None = Field(default=None, max_length=4096)
    watch: WatchSettings = Field(default_factory=WatchSettings)
    active_model_id: int | None = Field(default=None, ge=1)
    density: Literal["comfortable", "compact"] = "comfortable"
    page_size: int = Field(default=25, ge=1, le=1000)
    default_landing: str = Field(default="dashboard", min_length=1, max_length=100)

    @field_validator("resume_root_path", "default_landing", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value
