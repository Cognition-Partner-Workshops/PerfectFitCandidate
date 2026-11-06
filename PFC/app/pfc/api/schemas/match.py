"""CamelCase matching contracts for the Phase-1 client."""

from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class DemandIds(WireModel):
    demand_ids: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def positive(self) -> Self:
        if any(value <= 0 for value in self.demand_ids):
            raise ValueError("Demand IDs must be positive")
        return self


class RunRequest(WireModel):
    incremental: bool = False
    scope: Literal["full", "demands"] = "full"
    demand_ids: list[int] = Field(default_factory=list)
    as_of_date: date | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if any(value <= 0 for value in self.demand_ids):
            raise ValueError("Demand IDs must be positive")
        if len(set(self.demand_ids)) != len(self.demand_ids):
            raise ValueError("Demand IDs must be unique")
        if (self.scope == "demands") != bool(self.demand_ids):
            raise ValueError("Only demand scope requires demand IDs")
        return self


class JdItemSource(WireModel):
    kind: Literal["mandatory_skill", "preferred_skill", "experience", "qualification", "domain"]
    text: str
    jd_req_id: int | None = Field(default=None, strict=True)
    classification_reason: Literal["classification_adjusted"] | None = None


class JdRequirements(WireModel):
    demand_id: int = Field(gt=0)
    mandatory_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    experience_years: float | None = Field(default=None, ge=0)
    experience: list[str] | None = None
    qualifications: str = ""
    domain: str = ""
    location: str = ""
    mandatory_onsite: bool = False
    synonyms_applied: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    revision: int | None = None
    status: Literal["draft", "confirmed", "superseded"] | None = None
    extracted_by: str | None = None
    extraction_method: Literal["llm", "rule_based_fallback"] | None = None
    fallback_reason: str | None = None
    confirmed_at: str | None = None
    jd_req_id: int | None = None
    items: list[JdItemSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonblank_skills(self) -> Self:
        if any(not value.strip() for value in self.mandatory_skills + self.preferred_skills):
            raise ValueError("Skill text must not be blank")
        return self

    def extraction(self, previous_experience: list[dict[str, Any]]) -> dict[str, Any]:
        existing = {item["text"]: item for item in previous_experience}
        texts = self.experience
        if texts is None or texts == [item["text"] for item in previous_experience]:
            experience = [dict(item) for item in previous_experience]
        else:
            experience = [dict(existing.get(text, {"text": text})) for text in texts]
        old_max = max(
            (item["years"] for item in previous_experience if item.get("years") is not None),
            default=None,
        )
        if "experience_years" in self.model_fields_set and self.experience_years != old_max:
            years = self.experience_years
            if years is None:
                for item in experience:
                    item["years"] = None
            elif not experience:
                experience = [{"text": f"Minimum {years:g} years experience", "years": years}]
            else:
                targets = [
                    item
                    for item in experience
                    if old_max is not None and item.get("years") == old_max
                ]
                for item in targets or experience[:1]:
                    item["years"] = years
                for item in experience:
                    if item.get("years") is not None and item["years"] > years:
                        item["years"] = years
        result: dict[str, Any] = {
            "mandatory_skills": [{"text": term} for term in self.mandatory_skills],
            "preferred_skills": [{"text": term} for term in self.preferred_skills],
            "experience": experience,
            "qualifications": [
                {"text": value.strip()} for value in self.qualifications.split(";") if value.strip()
            ],
            "domain": [
                {"text": value.strip()} for value in self.domain.split(";") if value.strip()
            ],
            "location": self.location,
            "onsite_required": self.mandatory_onsite,
        }
        sources: dict[tuple[str, str], list[int | None]] = {}
        for source in self.items:
            text = (
                source.text.strip() if source.kind in {"qualification", "domain"} else source.text
            )
            sources.setdefault((source.kind, text), []).append(source.jd_req_id)
        for field, kind in (
            ("mandatory_skills", "mandatory_skill"),
            ("preferred_skills", "preferred_skill"),
            ("experience", "experience"),
            ("qualifications", "qualification"),
            ("domain", "domain"),
        ):
            for entry in result[field]:
                ids = sources.get((kind, entry["text"]), [])
                if ids:
                    entry["jdReqId"] = ids.pop(0)
        return result


def requirements_view(demand_id: int, row: dict[str, Any]) -> JdRequirements:
    data = row["extraction"]
    years = [item["years"] for item in data.get("experience", []) if item.get("years") is not None]
    synonyms = []
    for item in row["items"]:
        if item.get("canonical") and item["canonical"] != item["text"]:
            synonyms.append(f"{item['text']} → {item['canonical']}")
    return JdRequirements(
        demand_id=demand_id,
        mandatory_skills=[item["text"] for item in data.get("mandatory_skills", [])],
        preferred_skills=[item["text"] for item in data.get("preferred_skills", [])],
        experience_years=max(years) if years else None,
        experience=[item["text"] for item in data.get("experience", [])],
        qualifications="; ".join(item["text"] for item in data.get("qualifications", [])),
        domain="; ".join(item["text"] for item in data.get("domain", [])),
        location=data.get("location") or "",
        mandatory_onsite=bool(data.get("onsite_required")),
        synonyms_applied=synonyms,
        warnings=(
            [f"Rule-based fallback: {row.get('fallback_reason') or 'reason_not_recorded'}"]
            if row.get("extraction_method") == "rule_based_fallback"
            else []
        ),
        revision=row["revision"],
        status=row["status"],
        extracted_by=row["extracted_by"],
        extraction_method=row.get("extraction_method"),
        fallback_reason=row.get("fallback_reason"),
        confirmed_at=row["confirmed_at"],
        jd_req_id=row["jd_req_id"],
        items=[
            JdItemSource(
                kind=item["kind"],
                text=item["text"],
                jd_req_id=item["item_id"],
                classification_reason=item.get("classification_reason"),
            )
            for item in row["items"]
        ],
    )


class AnalysisFailure(WireModel):
    demand_id: int
    detail: str


class BulkAnalysis(WireModel):
    analyzed: list[JdRequirements]
    failed: list[AnalysisFailure]


class RunAccepted(WireModel):
    run_id: int
    job_id: int


class RunView(WireModel):
    run_id: int
    data_changed_since_run: bool
    data_changed_reasons: list[str]
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    finished_at: str | None
    llm_model: str
    template_version: int
    demands_assessed: int
    pairs_scored: int
    roles_without_candidate: int
    started_at: str | None
    engine_version: str
    config_hash: str
    template_id: int
    embedding_model_id: int
    scope: str
    dq_summary_json: str | None


def run_view(row: dict[str, Any]) -> RunView:
    fields = {name: row[name] for name in RunView.model_fields}
    for name in ("demands_assessed", "pairs_scored", "roles_without_candidate"):
        fields[name] = fields[name] or 0
    fields["llm_model"] = fields["llm_model"] or ""
    return RunView.model_validate(fields)
