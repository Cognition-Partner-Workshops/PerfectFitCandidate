"""REQ-IN-21/24: local CV synchronisation contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class CVFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: StrictStr
    evidence: StrictStr
    span: list[StrictInt] = Field(min_length=2, max_length=2)
    source: Literal["rule", "llm"]


class SkillFact(CVFact):
    canonical: str | None = None


class CVFacts(BaseModel):
    name: CVFact | None = None
    phone: CVFact | None = None
    experience: list[CVFact] = Field(default_factory=list)
    qualifications: list[CVFact] = Field(default_factory=list)
    projects: list[CVFact] = Field(default_factory=list)
    skills: list[SkillFact] = Field(default_factory=list)


class CVWarning(BaseModel):
    code: Literal["PHONE_NOT_FOUND"]
    message: str


class PublicCVFacts(CVFacts):
    warnings: list[CVWarning] = Field(default_factory=list)


class CVSyncRequest(BaseModel):
    resource_ids: list[int] | None = None


class CVSyncJob(BaseModel):
    job_id: int


class CVVersionItem(BaseModel):
    cv_version_id: int
    version_no: int
    file_name: str
    source_path: str
    file_mtime: str | None
    identity_match: str | None
    is_latest: bool
    extracted_at: str
    facts: PublicCVFacts | None = None


class CVSyncCounts(BaseModel):
    scanned: int = 0
    new_versions: int = 0
    unchanged: int = 0
    missing_cv: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    extraction_failed: int = 0
    extraction_timeout: int = 0
    too_large: int = 0
    embedded: int = 0
    warnings: int = 0
    facts_llm_failed: int = 0
    facts_llm_timeout: int = 0
