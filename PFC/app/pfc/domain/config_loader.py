"""Editable, immutable domain settings (REQ-SCR-01, REQ-ELIG-07, REQ-GRD-01)."""

import csv
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .normalise import SynonymTable, normalise_term


@dataclass(frozen=True)
class GradeLadder:
    codes: tuple[str, ...]

    def ordinal(self, grade: str) -> int:
        """REQ-GRD-01: return the one-based ordinal; unknown grades raise ValueError."""
        return self.codes.index(grade.strip().upper()) + 1

    def distance(self, first: str, second: str) -> int:
        return abs(self.ordinal(first) - self.ordinal(second))


@dataclass(frozen=True)
class BenchStatus:
    code: str
    label: str
    is_excluded: bool
    priority: int | None
    flag_in_gaps: bool
    available_from_bench_start: bool


@dataclass(frozen=True)
class BenchStatuses:
    statuses: Mapping[str, BenchStatus]
    status_map: Mapping[str, str]

    def resolve(self, source: str) -> BenchStatus | None:
        """REQ-ELIG-07: None means unmapped, never implicitly excluded."""
        code = self.status_map.get(normalise_term(source))
        return self.statuses.get(code) if code else None


@dataclass(frozen=True)
class LocationConfig:
    aliases: Mapping[str, str]
    travel_clusters: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ComponentWeights:
    skills: float
    grade: float
    location: float
    bench_start: float
    bench_end: float
    status: float


@dataclass(frozen=True)
class GradePoints:
    exact: float
    adjacent: float
    max_distance: int


@dataclass(frozen=True)
class SkillSubWeights:
    mandatory: float
    preferred: float
    semantic: float


@dataclass(frozen=True)
class SkillSettings:
    sub_weights: SkillSubWeights
    semantic_low: float
    semantic_high: float
    semantic_item_cap: float
    related_synonym_credit: float
    min_evidence_terms: int


@dataclass(frozen=True)
class BenchStartSettings:
    tiers_days: tuple[tuple[int, float], ...]
    beyond: float
    missing: float


@dataclass(frozen=True)
class WeakInclusion:
    enabled: bool
    requires_no_strong: bool
    min_transferable_semantic: float | None


@dataclass(frozen=True)
class ScoringTemplate:
    """Versioned REQ-SCR-01 template; nested collections are immutable."""

    name: str
    version: int
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

    def __post_init__(self) -> None:
        weights = vars(self.weights).values()
        if any(not math.isfinite(w) or w < 0 for w in weights) or sum(weights) != 100:
            raise ValueError("weights must be nonnegative and sum to 100")
        sub = vars(self.skills.sub_weights).values()
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
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
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
        object.__setattr__(self, "location_points", MappingProxyType(dict(self.location_points)))
        object.__setattr__(
            self, "status_tiered_points", MappingProxyType(dict(self.status_tiered_points))
        )
        object.__setattr__(self, "bands", MappingProxyType(dict(self.bands)))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScoringTemplate":
        """REQ-SCR-01: validate an editable template, raising a clear ValueError."""
        try:
            skills = dict(data["skills"])
            skills["sub_weights"] = SkillSubWeights(**skills["sub_weights"])
            bench = dict(data["bench_start"])
            bench["tiers_days"] = tuple(
                (int(day), float(point)) for day, point in bench["tiers_days"]
            )
            return cls(
                name=data["name"],
                version=data["version"],
                weights=ComponentWeights(**data["weights"]),
                grade=GradePoints(**data["grade"]),
                location_points=data["location_points"],
                skills=SkillSettings(**skills),
                bench_start=BenchStartSettings(**bench),
                status_score_mode=data["status_score_mode"],
                status_tiered_points={
                    int(k): float(v) for k, v in data["status_tiered_points"].items()
                },
                bands=data["bands"],
                top_n=data["top_n"],
                email_preselect_n=data["email_preselect_n"],
                weak_inclusion=WeakInclusion(**data["weak_inclusion"]),
                mandatory_gap_policy=data["mandatory_gap_policy"],
                unmapped_status_priority=data["unmapped_status_priority"],
                bench_end_blank_points=data["bench_end_blank_points"],
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid scoring template: {exc}") from exc

    @classmethod
    def from_json_file(cls, path: Path) -> "ScoringTemplate":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid scoring template JSON: {exc}") from exc
        return cls.from_dict(data)


@dataclass(frozen=True)
class DomainConfig:
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

    grades = GradeLadder(tuple(read_yaml("grades.yaml")))
    if not grades.codes or len(set(grades.codes)) != len(grades.codes):
        raise ValueError("grades must be nonempty and unique")
    statuses = {row["code"]: BenchStatus(**row) for row in read_yaml("bench_statuses.yaml")}
    status_map = {normalise_term(k): v for k, v in read_yaml("status_map.yaml").items()}
    if any(code not in statuses for code in status_map.values()):
        raise ValueError("status_map references an unknown bench status")
    locations = read_yaml("location_aliases.yaml")
    with (directory / "skill_synonyms.seed.csv").open(encoding="utf-8", newline="") as source:
        synonyms = SynonymTable(csv.DictReader(source))
    return DomainConfig(
        grades,
        BenchStatuses(MappingProxyType(statuses), MappingProxyType(status_map)),
        synonyms,
        LocationConfig(
            MappingProxyType(
                {normalise_term(k): normalise_term(v) for k, v in locations["aliases"].items()}
            ),
            MappingProxyType(
                {
                    k: tuple(normalise_term(v) for v in values)
                    for k, values in locations["travel_clusters"].items()
                }
            ),
        ),
        ScoringTemplate.from_json_file(directory / "scoring_template.default.json"),
    )
