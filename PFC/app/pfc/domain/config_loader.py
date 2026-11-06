"""Editable, immutable domain settings (REQ-SCR-01, REQ-ELIG-07, REQ-GRD-01)."""

import csv
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .normalise import SynonymTable, normalise_term


class FrozenConfig(BaseModel):
    """REQ-SCR-01: validated immutable settings, including nested mappings."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def freeze_mappings(cls, value: Any) -> Any:
        return MappingProxyType(dict(value)) if isinstance(value, Mapping) else value

    @field_serializer("*")
    def serialize_mappings(self, value: Any) -> Any:
        return dict(value) if isinstance(value, Mapping) else value


class GradeLadder(FrozenConfig):
    codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_codes(self) -> Self:
        if len(set(self.codes)) != len(self.codes):
            raise ValueError("grades must be unique")
        return self

    def ordinal(self, grade: str) -> int:
        """REQ-GRD-01: return the one-based ordinal; unknown grades raise ValueError."""
        return self.codes.index(grade.strip().upper()) + 1

    def distance(self, first: str, second: str) -> int:
        return abs(self.ordinal(first) - self.ordinal(second))


class BenchStatus(FrozenConfig):
    code: str
    label: str
    is_excluded: bool
    priority: int | None
    flag_in_gaps: bool
    available_from_bench_start: bool


class BenchStatuses(FrozenConfig):
    statuses: Mapping[str, BenchStatus]
    status_map: Mapping[str, str]

    @model_validator(mode="after")
    def validate_status_map(self) -> Self:
        if any(code not in self.statuses for code in self.status_map.values()):
            raise ValueError("status_map references an unknown bench status")
        return self

    def resolve(self, source: str) -> BenchStatus | None:
        """REQ-ELIG-07: None means unmapped, never implicitly excluded."""
        code = self.status_map.get(normalise_term(source))
        return self.statuses.get(code) if code else None


class LocationConfig(FrozenConfig):
    aliases: Mapping[str, str]
    travel_clusters: Mapping[str, tuple[str, ...]]


class ComponentWeights(FrozenConfig):
    skills: float
    grade: float
    location: float
    bench_start: float
    bench_end: float
    status: float


class GradePoints(FrozenConfig):
    exact: float
    adjacent: float
    max_distance: int


class SkillSubWeights(FrozenConfig):
    mandatory: float
    preferred: float
    semantic: float


class SkillSettings(FrozenConfig):
    sub_weights: SkillSubWeights
    semantic_low: float
    semantic_high: float
    semantic_item_cap: float = Field(ge=0, le=1)
    related_synonym_credit: float = Field(ge=0, le=1)
    min_evidence_terms: int


class BenchStartSettings(FrozenConfig):
    tiers_days: tuple[tuple[int, float], ...]
    beyond: float
    missing: float


class WeakInclusion(FrozenConfig):
    enabled: bool
    requires_no_strong: bool
    min_transferable_semantic: float | None


class ScoringTemplate(FrozenConfig):
    """Versioned REQ-SCR-01 template; nested collections are immutable."""

    name: str
    version: int = Field(strict=True, ge=1)
    weights: ComponentWeights
    grade: GradePoints
    location_points: Mapping[str, float]
    skills: SkillSettings
    bench_start: BenchStartSettings
    status_score_mode: str
    status_tiered_points: Mapping[int, float]
    bands: Mapping[str, float]
    top_n: int
    email_preselect_n: int
    weak_inclusion: WeakInclusion
    mandatory_gap_policy: str
    unmapped_status_priority: int
    bench_end_blank_points: float

    @model_validator(mode="after")
    def validate_template(self) -> Self:
        weights = self.weights.model_dump().values()
        if any(not math.isfinite(w) or w < 0 for w in weights) or sum(weights) != 100:
            raise ValueError("weights must be nonnegative and sum to 100")
        sub = self.skills.sub_weights.model_dump().values()
        if any(not math.isfinite(w) or w < 0 for w in sub) or not math.isclose(
            sum(sub), 1.0, rel_tol=0, abs_tol=1e-9
        ):
            raise ValueError("skills sub_weights must sum to 1.0")
        if self.status_score_mode not in {"binary", "tiered"}:
            raise ValueError("status_score_mode must be binary or tiered")
        if self.mandatory_gap_policy not in {"flag_only", "cap_band"}:
            raise ValueError("mandatory_gap_policy must be flag_only or cap_band")
        if set(self.bands) != {"Strong", "Potential", "Avg"} or not (
            100 >= self.bands["Strong"] > self.bands["Potential"] > self.bands["Avg"] >= 0
        ):
            raise ValueError("bands must descend Strong > Potential > Avg within 0..100")
        component_points = {
            "grade": (self.grade.exact, self.grade.adjacent),
            "location": tuple(self.location_points.values()),
            "bench_start": (
                *(point for _, point in self.bench_start.tiers_days),
                self.bench_start.beyond,
                self.bench_start.missing,
            ),
            "bench_end": (self.bench_end_blank_points,),
            "status": tuple(self.status_tiered_points.values()),
        }
        for component, points in component_points.items():
            maximum = getattr(self.weights, component)
            if any(not math.isfinite(point) or not 0 <= point <= maximum for point in points):
                raise ValueError(f"{component} points must be within 0..weights.{component}")
        return self

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScoringTemplate":
        """REQ-SCR-01: validate an editable template, raising a clear ValueError."""
        try:
            return cls.model_validate(data)
        except ValueError as exc:
            raise ValueError(f"Invalid scoring template: {exc}") from exc

    @classmethod
    def from_json_file(cls, path: Path) -> "ScoringTemplate":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid scoring template JSON: {exc}") from exc
        return cls.from_dict(data)


class DomainConfig(FrozenConfig):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    grades: GradeLadder
    bench_statuses: BenchStatuses
    synonyms: SynonymTable
    locations: LocationConfig
    template: ScoringTemplate


def load_domain_config(config_dir: Path | None = None) -> DomainConfig:
    """Load REQ-SCR-01/REQ-MAT-05 defaults, optionally overridden by PFC_CONFIG_DIR."""
    directory = (
        config_dir
        if config_dir is not None
        else Path(os.environ.get("PFC_CONFIG_DIR", Path(__file__).resolve().parents[3] / "config"))
    )

    def read_yaml(name: str) -> Any:
        return yaml.safe_load((directory / name).read_text(encoding="utf-8"))

    grades = GradeLadder(codes=tuple(read_yaml("grades.yaml")))
    statuses = {row["code"]: BenchStatus(**row) for row in read_yaml("bench_statuses.yaml")}
    status_map = {normalise_term(k): v for k, v in read_yaml("status_map.yaml").items()}
    bench_statuses = BenchStatuses(statuses=statuses, status_map=status_map)
    locations = read_yaml("location_aliases.yaml")
    with (directory / "skill_synonyms.seed.csv").open(encoding="utf-8", newline="") as source:
        synonyms = SynonymTable(csv.DictReader(source))
    return DomainConfig(
        grades=grades,
        bench_statuses=bench_statuses,
        synonyms=synonyms,
        locations=LocationConfig(
            aliases={normalise_term(k): normalise_term(v) for k, v in locations["aliases"].items()},
            travel_clusters={
                k: tuple(normalise_term(v) for v in values)
                for k, values in locations["travel_clusters"].items()
            },
        ),
        template=ScoringTemplate.from_json_file(directory / "scoring_template.default.json"),
    )
