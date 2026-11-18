"""REQ-IN-24 / FR-ING-52: read-only CV version comparison contracts (masked by default)."""

from typing import Literal

from pfc.api.schemas.match import WireModel

DiffStatus = Literal["added", "removed", "changed", "unchanged"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class CompareVersion(WireModel):
    cv_version_id: int
    version_no: int
    file_name: str
    modified_at: str | None
    extracted_at: str
    is_latest: bool
    extraction_removed: bool


class CompareValue(WireModel):
    value: str
    evidence: str
    source: str | None = None
    raw: str | None = None


class CompareFact(WireModel):
    key: str
    status: DiffStatus
    left: CompareValue | None
    right: CompareValue | None


class CompareListItem(CompareFact):
    label: str


class CompareFacts(WireModel):
    summary: list[CompareFact]
    years_delta: float | None
    skills: list[CompareListItem]
    experience: list[CompareListItem]
    qualifications: list[CompareListItem]
    projects: list[CompareListItem]
    facts_extracted: list[bool]


class DiffLine(WireModel):
    no: int
    text: str


class DiffRow(WireModel):
    op: Literal["equal", "added", "removed", "changed", "skip"]
    left: DiffLine | None
    right: DiffLine | None
    count: int | None = None


class TextDiff(WireModel):
    added: int
    removed: int
    unchanged: int
    rows: list[DiffRow]
    truncated: bool


class StoredVersionScore(WireModel):
    score: float
    band: str


class StoredScore(WireModel):
    result_id: int
    run_id: int
    demand_id: int
    code_requisition: str
    role: str | None
    resource_id: int
    short_name: str
    score: float
    band: str
    best_cv_version_id: int | None
    left: StoredVersionScore | None
    right: StoredVersionScore | None


class CvComparison(WireModel):
    candidate_id: int
    left: CompareVersion
    right: CompareVersion
    facts: CompareFacts
    text_diff: TextDiff
    stored_scores: list[StoredScore]


class CvCompareRequest(WireModel):
    left_cv_version_id: int
    right_cv_version_id: int


class CvCompareJob(WireModel):
    comparison_id: int
    job_id: int


class ScoreComponent(WireModel):
    component: str
    points: float
    max_points: float


class ScoreItem(WireModel):
    item_id: str
    text: str
    kind: str
    credit: float
    method: str
    evidence: str


class ExclusionItem(WireModel):
    code: str
    detail: str


class VersionScore(WireModel):
    score: float | None
    band: str | None
    basis: str | None
    components: list[ScoreComponent]
    items: list[ScoreItem]
    mandatory_missing: int
    gaps: list[str]
    excluded: bool
    reasons: list[ExclusionItem]


class DemandScoreDelta(WireModel):
    demand_id: int
    code_requisition: str
    role: str | None
    resource_id: int
    short_name: str
    as_of_date: str
    stored_score: float
    stored_band: str
    stored_best_cv_version_id: int | None
    left: VersionScore
    right: VersionScore
    delta: float | None


class ScoringResult(WireModel):
    template_name: str
    template_version: int
    skipped_demand_ids: list[int]
    demands: list[DemandScoreDelta]


class CvCompareScoring(WireModel):
    comparison_id: int
    job_id: int | None
    status: JobStatus
    created_at: str
    finished_at: str | None
    result: ScoringResult | None
