"""Frozen scoring inputs and explainable results (REQ-SCR-04, REQ-ELIG-06)."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Literal

from ..normalise import ParsedDate


@dataclass(frozen=True)
class Resource:
    resource_id: str
    short_name: str
    grade: str | None
    location: str | None
    bench_status: str | None
    group: str | None
    bench_start: ParsedDate
    bench_end: ParsedDate
    ct_skills: str | None
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        if self.candidate_id is None:
            object.__setattr__(self, "candidate_id", self.resource_id)


@dataclass(frozen=True)
class SkillEvidence:
    """REQ-MAT-06: an explicit skill assertion with its unmodified source evidence."""

    raw: str
    normalised: str
    canonical: str | None = None
    evidence: str = ""
    canonical_skill_id: int | None = None


@dataclass(frozen=True)
class SkillBasis:
    basis_id: str
    label: str
    skills: tuple[SkillEvidence, ...]
    is_latest: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "skills", tuple(self.skills))


@dataclass(frozen=True)
class RequirementItem:
    item_id: str
    text: str
    kind: Literal["mandatory", "preferred"]
    skill_term: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"mandatory", "preferred"}:
            raise ValueError("Requirement kind must be mandatory or preferred")


@dataclass(frozen=True)
class Demand:
    code_requisition: str
    position_start: ParsedDate
    grade: str | None
    region: str | None
    onsite_required: bool
    requirements: tuple[RequirementItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirements", tuple(self.requirements))
        ids = [item.item_id for item in self.requirements]
        if len(set(ids)) != len(ids):
            raise ValueError("Requirement item IDs must be unique")


@dataclass(frozen=True)
class SemanticInput:
    """REQ-MAT-01: caller-normalised semantic scores, keyed by demand and basis."""

    overall: float = 0.0
    item_credits: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(
            not math.isfinite(v) or not 0 <= v <= 1
            for v in (self.overall, *self.item_credits.values())
        ):
            raise ValueError("Semantic scores must be finite and within 0..1")
        object.__setattr__(self, "item_credits", MappingProxyType(dict(self.item_credits)))


@dataclass(frozen=True)
class ExclusionReason:
    code: str
    detail: str


@dataclass(frozen=True)
class Exclusion:
    resource_id: str
    code_requisition: str | None
    reasons: tuple[ExclusionReason, ...]


@dataclass(frozen=True)
class Component:
    points: float
    maximum: float
    inputs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", round(self.points, 4))


@dataclass(frozen=True)
class ItemResult:
    item_id: str
    kind: str
    credit: float
    method: str
    evidence: str


@dataclass(frozen=True)
class BasisResult:
    basis_id: str
    label: str
    is_latest: bool
    score: float
    band: str
    components: Mapping[str, Component]
    items: tuple[ItemResult, ...]
    mandatory_missing: int
    recognised_skills: int
    semantic: float
    matching_evidence: str
    gaps: tuple[str, ...]
    eligibility_evidence: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))


@dataclass(frozen=True)
class PersonResult:
    resource: Resource
    best: BasisResult
    per_basis: tuple[BasisResult, ...]
    status_priority: int
    grade_distance: int

    @property
    def display_band(self) -> str:
        return "Weak Match" if self.best.band == "Weak" else self.best.band

    @property
    def gaps_checks(self) -> str:
        return "; ".join(self.best.gaps)


@dataclass(frozen=True)
class DemandResult:
    demand: Demand
    rows: tuple[PersonResult, ...]
    insufficient_data: tuple[PersonResult, ...]
    shortlist: tuple[PersonResult, ...]
    email_preselected: tuple[PersonResult, ...]
    flags: tuple[str, ...]
    no_suitable_candidate: bool
    weak_only: bool


@dataclass(frozen=True)
class MatchRun:
    demands: tuple[DemandResult, ...]
    global_exclusions: tuple[Exclusion, ...]
    pair_exclusions: tuple[Exclusion, ...]
    requisitions_assessed: int
    pairs_scored: int
    roles_without_suitable_candidate: int
    weak_only_roles: int
    template_name: str
    template_version: int
    engine_version: str
    as_of_date: date
