"""Matching service API; adapters and HTTP are supplied by callers."""

from .ports import ChunkOwner, Embedder, LlmProvider, MatchingPorts, VectorStore
from .requirements import analyze_jd, confirm_requirements, get_requirements, save_draft
from .results import (
    ResultFilters,
    add_override,
    demand_matches,
    excluded_current_view,
    excluded_view,
    get_run,
    list_results,
    list_runs,
    result_detail,
    revoke_override,
)
from .runs import start_run

__all__ = [
    "ChunkOwner",
    "Embedder",
    "LlmProvider",
    "MatchingPorts",
    "VectorStore",
    "analyze_jd",
    "confirm_requirements",
    "get_requirements",
    "save_draft",
    "ResultFilters",
    "add_override",
    "excluded_current_view",
    "revoke_override",
    "demand_matches",
    "excluded_view",
    "get_run",
    "list_results",
    "list_runs",
    "result_detail",
    "start_run",
]
