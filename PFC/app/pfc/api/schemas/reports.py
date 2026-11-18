from typing import Literal, Self

from pydantic import Field, model_validator

from pfc.api.schemas.analytics import DashboardFilters
from pfc.api.schemas.match import WireModel
from pfc.api.schemas.results import ResultsQuery

Section = Literal[
    "cover",
    "summary",
    "demand",
    "bench",
    "match",
    "unmatched",
    "extended-other",
    "data-quality-method",
    "match-results",
]
DASHBOARD_SECTIONS: tuple[Section, ...] = (
    "cover",
    "summary",
    "demand",
    "bench",
    "match",
    "unmatched",
    "extended-other",
    "data-quality-method",
)


class PdfRequest(WireModel):
    report: Literal["dashboard", "match_results"]
    scope: Literal["filters", "all"] = "filters"
    sections: list[Section] = Field(default_factory=list)
    orientation: Literal["portrait", "landscape"] = "portrait"
    filters: DashboardFilters = Field(default_factory=DashboardFilters)
    result_filters: ResultsQuery = Field(default_factory=ResultsQuery)

    @model_validator(mode="after")
    def valid_sections(self) -> Self:
        if self.report == "match_results" and "orientation" not in self.model_fields_set:
            self.orientation = "landscape"
        allowed = DASHBOARD_SECTIONS if self.report == "dashboard" else ("cover", "match-results")
        if not self.sections:
            self.sections = list(allowed)
        if len(set(self.sections)) != len(self.sections) or any(
            s not in allowed for s in self.sections
        ):
            raise ValueError("Invalid or duplicate sections for this report")
        if self.report == "match_results" and set(self.sections) != set(allowed):
            raise ValueError("Match results reports require cover and match-results")
        self.sections = [s for s in allowed if s in self.sections]
        return self

    def dashboard_filters(self) -> DashboardFilters:
        return DashboardFilters() if self.scope == "all" else self.filters

    def results_query(self) -> ResultsQuery:
        return ResultsQuery() if self.scope == "all" else self.result_filters


class PdfAccepted(WireModel):
    job_id: int
