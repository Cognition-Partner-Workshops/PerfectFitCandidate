from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["Admin", "Analyst"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserCreate(Input):
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    role: Role
    persona: Literal["A", "B", "C"] | None = None


class UserUpdate(Input):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    persona: Literal["A", "B", "C"] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def nonnull(self) -> Self:
        for key in self.model_fields_set - {"persona"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        return self


class BenchStatus(Input):
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    is_excluded: bool
    priority: int | None = Field(default=None, ge=1)
    flag_in_gaps: bool = False
    available_from_bench_start: bool = False

    @model_validator(mode="after")
    def priority_valid(self) -> Self:
        if self.is_excluded != (self.priority is None):
            raise ValueError(
                "Excluded statuses require null priority; eligible ones require priority"
            )
        return self


class StatusMap(Input):
    canonical_code: str | None = None
    priority: int = Field(ge=1)
    is_excluded: bool


class Grade(Input):
    code: str = Field(min_length=1, pattern=r".*\S.*")
    active: bool = True


class Locations(Input):
    aliases: dict[str, str]
    travel_clusters: dict[str, list[str]]

    @model_validator(mode="after")
    def normalize(self) -> Self:
        self.aliases = {k.strip().lower(): v.strip().lower() for k, v in self.aliases.items()}
        self.travel_clusters = {
            k.strip().lower(): [v.strip().lower() for v in values]
            for k, values in self.travel_clusters.items()
        }
        if any(not k or not v for k, v in self.aliases.items()) or any(
            not k or any(not v for v in values) for k, values in self.travel_clusters.items()
        ):
            raise ValueError("Location names must not be empty")
        return self


class Synonym(Input):
    canonical: str = Field(min_length=1)
    term: str = Field(min_length=1)
    relation: Literal["equivalent", "abbreviation", "misspelling", "related"]
    credit: float = Field(default=1, ge=0, le=1)
    status: Literal["suggested", "approved", "rejected"] = "approved"

    @model_validator(mode="after")
    def related_credit(self) -> Self:
        if self.relation == "related" and self.credit >= 1:
            raise ValueError("Related terms require credit below 1")
        return self


class ApproveSynonym(Input):
    canonical: str | None = Field(default=None, min_length=1)


class TemplateCreate(Input):
    clone_from_id: int | None = None
    name: str | None = Field(default=None, min_length=1)
    weights: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None
    change_note: str = Field(min_length=1)


class TemplateUpdate(Input):
    weights: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None
    change_note: str = Field(min_length=1)


class EmailTemplate(Input):
    name: str = Field(min_length=1)
    subject: str
    body_html: str


class Recipient(Input):
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=320)
    display_name: str | None = None


class RecipientUpdate(Input):
    display_name: str | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def active_nonnull(self) -> Self:
        if "is_active" in self.model_fields_set and self.is_active is None:
            raise ValueError("is_active cannot be null")
        return self


class Domain(Input):
    domain: str


class AuditFilters(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    action: list[str] = []
    user_id: int | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    format: Literal["csv"] = "csv"

    @model_validator(mode="after")
    def times(self) -> Self:
        if any(value is not None and value.tzinfo is None for value in (self.from_, self.to)):
            raise ValueError("Audit timestamps require a timezone")
        if self.from_ and self.to and self.from_ > self.to:
            raise ValueError("from must not exceed to")
        return self
