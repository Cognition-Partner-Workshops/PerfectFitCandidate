import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pfc.api.schemas.analytics import (
    AnalyticsSummary,
    BenchAnalytics,
    DemandAnalytics,
    ExtendedAnalytics,
    MatchAnalytics,
)
from pfc.api.schemas.reports import PdfRequest
from pfc.services import matching
from pfc.services.analytics import dashboard, extended
from pfc.services.analytics.population import Population
from pfc.services.matching.current import current_runs

MAX_RESULTS = 5000


@dataclass
class ReportData:
    request: PdfRequest
    generated_by: str
    generated_at: datetime
    panels: dict[str, Any]
    results: list[dict[str, Any]]
    total: int
    runs: list[dict[str, Any]]
    sources: list[dict[str, Any]]


def collect(
    db: sqlite3.Connection, request: PdfRequest, username: str, now: datetime
) -> ReportData:
    panels: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    total = 0
    runs: list[dict[str, Any]] = []
    if request.report == "dashboard":
        p = Population(db, request.dashboard_filters(), now.date())
        panels = {
            "summary": AnalyticsSummary(**dashboard.summary(p)).model_dump(by_alias=True),
            "demand": DemandAnalytics(**dashboard.demand(p)).model_dump(by_alias=True),
            "bench": BenchAnalytics(**dashboard.bench(p)).model_dump(by_alias=True),
            "match": MatchAnalytics(**dashboard.match(p)).model_dump(by_alias=True),
            "extended": ExtendedAnalytics(**extended.build(p)).model_dump(by_alias=True),
        }
        total = len(panels["demand"]["rows"])
        runs = [p.runs[k] for k in sorted(p.runs)]
    else:
        query = request.results_query()
        page = matching.list_results(
            db,
            filters=query.model_dump(exclude={"sort", "descending", "limit", "offset"}),
            sort=query.sort,
            descending=query.descending,
            limit=min(query.limit or MAX_RESULTS, MAX_RESULTS),
            offset=query.offset,
            run_pairs=current_runs(db),
        )
        results, total = page["items"], page["total"]
        runs = [
            run
            for rid in sorted({r["run_id"] for r in results})
            if (run := matching.get_run(db, rid)) is not None
        ]
    for run in runs:
        model = db.execute(
            "SELECT name,file_sha256,pipeline_version FROM embedding_models "
            "WHERE embedding_model_id=?",
            (run["embedding_model_id"],),
        ).fetchone()
        run["embedding_model"] = dict(model) if model else {}
    sources = [
        dict(row)
        for row in db.execute(
            "SELECT kind,file_name,file_sha256,started_at FROM ingestion_runs "
            "WHERE kind IN ('DL','CT') AND status='committed' "
            "ORDER BY kind,ingestion_run_id"
        )
    ]
    return ReportData(request, username, now, panels, results, total, runs, sources)
