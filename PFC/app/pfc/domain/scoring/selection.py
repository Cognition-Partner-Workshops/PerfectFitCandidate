"""Post-score selection shared by full and incremental matching."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ..config_loader import ScoringTemplate
from .models import PersonResult, Resource


@dataclass(frozen=True)
class RankingInput:
    resource: Resource
    score: float
    band: str
    status_priority: int
    grade_distance: int
    mandatory_missing: int
    semantic: float
    transferable: bool

    @classmethod
    def from_result(cls, row: PersonResult) -> "RankingInput":
        return cls(
            row.resource,
            row.best.score,
            row.best.band,
            row.status_priority,
            row.grade_distance,
            row.best.mandatory_missing,
            row.best.semantic,
            any(i.kind == "mandatory" and i.credit > 0 for i in row.best.items),
        )


def input_key(row: RankingInput) -> tuple[float, int, int, date, int, str, str]:
    start = row.resource.bench_start
    return (
        -row.score,
        row.status_priority,
        row.grade_distance,
        start.value if start.status == "valid" and start.value else date.max,
        row.mandatory_missing,
        row.resource.short_name.casefold(),
        row.resource.resource_id,
    )


@dataclass(frozen=True)
class Selection:
    sufficient: tuple[RankingInput, ...]
    insufficient: tuple[RankingInput, ...]
    shortlist: tuple[RankingInput, ...]
    emails: tuple[RankingInput, ...]
    no_suitable: bool
    weak_only: bool


def select_scored_rows(rows: Iterable[RankingInput], template: ScoringTemplate) -> Selection:
    unique: dict[str | None, RankingInput] = {}
    for row in sorted(rows, key=input_key):
        unique.setdefault(row.resource.candidate_id, row)
    ranked = tuple(unique.values())
    sufficient = tuple(row for row in ranked if row.band != "Insufficient data")
    insufficient = tuple(row for row in ranked if row.band == "Insufficient data")
    suitable = [row for row in sufficient if row.band in {"Strong", "Potential", "Avg"}]
    shortlist = suitable[: template.top_n]
    strong = any(row.band == "Strong" for row in sufficient)
    weak = template.weak_inclusion
    if weak.enabled and (not weak.requires_no_strong or not strong):
        for row in sufficient:
            threshold = weak.min_transferable_semantic
            if (
                row.band == "Weak"
                and row.transferable
                and (threshold is None or row.semantic >= threshold)
                and len(shortlist) < template.top_n
            ):
                shortlist.append(row)
    return Selection(
        sufficient,
        insufficient,
        tuple(shortlist),
        tuple(shortlist[: template.email_preselect_n]),
        not suitable,
        bool(shortlist) and all(row.band == "Weak" for row in shortlist),
    )
