"""Frontend result rows and audited, non-destructive overrides."""

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from pfc.api.schemas.analytics import ExportQuery
from pfc.api.schemas.match import run_view
from pfc.api.schemas.results import (
    BreakdownView,
    CurrentDemand,
    CurrentResults,
    CvScoreView,
    ExcludedRow,
    OverrideReason,
    OverrideRequest,
    OverrideView,
    ResultDetail,
    ResultRow,
    ResultsQuery,
    band_label,
)
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.services import matching
from pfc.services.matching.current import current_runs
from pfc.services.matching.data_changes import REASONS, stale_demand_ids_for_pairs
from pfc.services.matching.export import evidence_parts, export_results
from pfc.services.pii import mask_emails_in, require_unmask_permission

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


def find_run(db: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = matching.get_run(db, run_id)
    if run is None:
        raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
    return run


def override_views(db: sqlite3.Connection, result_id: int) -> list[OverrideView]:
    return [
        OverrideView(
            action=row["action"],
            new_band=band_label(row["new_band"]) if row["new_band"] else None,
            reason=row["reason"],
            created_by=row["display_name"],
            created_at=row["created_at"],
            override_id=row["override_id"],
            revoked_at=row["revoked_at"],
        )
        for row in db.execute(
            "SELECT o.*,u.display_name FROM overrides o JOIN users u ON u.user_id=o.created_by "
            "WHERE result_id=? ORDER BY override_id",
            (result_id,),
        )
    ]


def result_view(db: sqlite3.Connection, row: dict[str, Any], *, detail: bool = False) -> ResultRow:
    components = row.get("breakdown")
    if components is None:
        components = [
            dict(value)
            for value in db.execute(
                "SELECT component,points,max_points FROM score_breakdowns WHERE result_id=? "
                "ORDER BY CASE component WHEN 'skills' THEN 0 WHEN 'grade' THEN 1 WHEN 'location' "
                "THEN 2 WHEN 'bench_start' THEN 3 WHEN 'bench_end' THEN 4 ELSE 5 END",
                (row["result_id"],),
            )
        ]
    overrides = override_views(db, row["result_id"])
    active = [value for value in overrides if value.revoked_at is None]
    exclusions = [value for value in active if value.action == "exclude"]
    adjustments = [value for value in active if value.action == "adjust_band"]
    preferred = exclusions or adjustments or active
    fields = {
        key: row[key]
        for key in (
            "result_id",
            "run_id",
            "demand_id",
            "resource_id",
            "rank",
            "bu",
            "demand_grade",
            "person_grade",
            "person_location",
            "score",
            "grade_relation",
            "location_assessment",
            "mandatory_missing",
            "candidate_type",
            "incomplete_identity",
            "effective_shortlisted",
            "effective_excluded",
            "shortlisted",
        )
    }
    fields.update(
        data_changed_since_run=row["data_changed_since_run"],
        data_changed_reasons=row["data_changed_reasons"],
        candidate_id=row["candidate_id"] or 0,
        code_requisition=row["requisition"],
        position_start_date=row["start_date"],
        account_name=row["account"],
        demand_request_name=row["role"],
        region=row["role_location"],
        person_name=row["potential_person"],
        person_email_masked=row["person_email"],
        bench_status=row["status"],
        band=band_label(row["band"]),
        effective_band=band_label(row["effective_band"]),
        email_preselected=row["effective_email_preselected"],
        breakdown=[
            BreakdownView(
                component=value["component"],
                points=value["points"],
                max_points=value["max_points"],
                inputs=value.get("inputs") if detail else None,
            )
            for value in components
        ],
        override=preferred[-1] if preferred else None,
        active_overrides=active,
    )
    for key in ("matching_evidence", "gaps_checks", "eligibility_evidence"):
        fields[key] = [mask_emails_in(part) for part in evidence_parts(row[key])]
    if detail:
        fields["cv_scores"] = [
            CvScoreView(
                cv_version_id=value["cv_version_id"],
                score=value["score"],
                band=band_label(value["band"]),
                version_no=value["version_no"],
                is_latest=value["is_latest"],
            )
            for value in row["cv_scores"]
        ]
        fields["overrides"] = overrides
        return ResultDetail.model_validate(fields)
    return ResultRow.model_validate(fields)


def required_result(db: sqlite3.Connection, result_id: int) -> dict[str, Any]:
    row = matching.result_detail(db, result_id)
    if row is None:
        raise Problem(404, "RESULT_NOT_FOUND", "Match result not found")
    run = find_run(db, row["run_id"])
    row.update(
        data_changed_since_run=run["data_changed_since_run"],
        data_changed_reasons=run["data_changed_reasons"],
    )
    return row


@router.get("/match-runs/{run_id}/results", response_model=list[ResultRow])
def results(
    run_id: int, request: Request, response: Response, query: Annotated[ResultsQuery, Query()]
) -> list[ResultRow]:
    filters = query.model_dump(exclude={"sort", "descending", "limit", "offset"})
    with database(request.app.state.settings) as db:
        run = find_run(db, run_id)
        page = matching.list_results(
            db,
            run_id,
            filters,
            sort=query.sort,
            descending=query.descending,
            limit=query.limit,
            offset=query.offset,
        )
        response.headers["X-Total-Count"] = str(page["total"])
        response.headers["X-Data-Changed-Since-Run"] = json.dumps(run["data_changed_since_run"])
        response.headers["X-Data-Changed-Reasons"] = json.dumps(run["data_changed_reasons"])
        for row in page["items"]:
            row.update(
                data_changed_since_run=run["data_changed_since_run"],
                data_changed_reasons=run["data_changed_reasons"],
            )
        return [result_view(db, row) for row in page["items"]]


@router.get("/match-runs/{run_id}/export")
def export(
    run_id: int, request: Request, principal: Analyst, query: Annotated[ExportQuery, Query()]
) -> Response:
    filters = query.model_dump(
        exclude={"format", "include_emails", "sort", "descending", "limit", "offset"}
    )
    with database(request.app.state.settings) as db:
        if query.include_emails:
            require_unmask_permission(db, principal.session.role_at_login)
        content, media = export_results(
            db,
            run_id,
            query.format,
            filters,
            user_id=principal.user.user_id,
            now=request.app.state.clock(),
            include_emails=query.include_emails,
            sort=query.sort,
            descending=query.descending,
            limit=query.limit,
            offset=query.offset,
        )
    return Response(
        content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="PFC-results-run{run_id}.{query.format}"'
        },
    )


@router.get("/match-results/current", response_model=list[ResultRow])
def current_results(
    request: Request, response: Response, query: Annotated[ResultsQuery, Query()]
) -> list[ResultRow]:
    filters = query.model_dump(exclude={"sort", "descending", "limit", "offset"})
    with database(request.app.state.settings) as db:
        pairs = current_runs(db)
        page = matching.list_results(
            db,
            filters=filters,
            sort=query.sort,
            descending=query.descending,
            limit=query.limit,
            offset=query.offset,
            run_pairs=pairs,
        )
        response.headers["X-Total-Count"] = str(page["total"])
        runs = {
            run_id: matching.get_run(db, run_id)
            for run_id in set(row["run_id"] for row in page["items"])
        }
        for row in page["items"]:
            run = runs[row["run_id"]]
            if run is not None:
                row.update(
                    data_changed_since_run=run["data_changed_since_run"],
                    data_changed_reasons=run["data_changed_reasons"],
                )
        return [result_view(db, row) for row in page["items"]]


@router.get("/match-results/current/summary", response_model=CurrentResults)
def current_results_summary(request: Request) -> CurrentResults:
    with database(request.app.state.settings) as db:
        pairs = current_runs(db)
        run_ids = sorted(set(pairs.values()), reverse=True)
        run_records = {
            run_id: run for run_id in run_ids if (run := matching.get_run(db, run_id)) is not None
        }
        demand_rows = {
            int(row["demand_id"]): dict(row)
            for row in db.execute(
                "SELECT demand_id,code_requisition FROM demands WHERE lifecycle='open'"
            )
        }
        demands = [
            CurrentDemand(
                demand_id=demand_id,
                code_requisition=str(demand_rows[demand_id]["code_requisition"]),
                run_id=run_id,
                finished_at=str(run_records[run_id]["finished_at"] or ""),
            )
            for demand_id, run_id in pairs.items()
            if demand_id in demand_rows and run_id in run_records
        ]
        demands.sort(key=lambda item: (item.code_requisition.casefold(), item.demand_id))
        stale_demand_ids = stale_demand_ids_for_pairs(db, pairs, run_records)
        reasons = [
            reason
            for reason in REASONS
            if any(reason in run["data_changed_reasons"] for run in run_records.values())
        ]
        return CurrentResults(
            demands=demands,
            runs=[run_view(run_records[run_id]) for run_id in run_ids if run_id in run_records],
            data_changed_since_run=any(
                run["data_changed_since_run"] for run in run_records.values()
            ),
            data_changed_reasons=reasons,
            stale_demand_ids=stale_demand_ids,
        )


@router.get("/match-results/current/excluded", response_model=list[ExcludedRow])
def current_excluded(
    request: Request,
    requisition: Annotated[list[str] | None, Query()] = None,
) -> list[ExcludedRow]:
    with database(request.app.state.settings) as db:
        rows = matching.excluded_current_view(db, current_runs(db), requisition)
        return [
            ExcludedRow(
                run_id=row["run_id"],
                resource_id=row["resource_id"],
                short_name=row["short_name"],
                demand_id=row["demand_id"],
                code_requisition=row["requisition"],
                reason_code=row["reason_code"],
                detail=row["detail"],
                scope="global" if row["demand_id"] is None else "pair",
            )
            for row in rows
        ]


@router.get("/match-results/current/export")
def export_current(
    request: Request, principal: Analyst, query: Annotated[ExportQuery, Query()]
) -> Response:
    filters = query.model_dump(
        exclude={"format", "include_emails", "sort", "descending", "limit", "offset"}
    )
    with database(request.app.state.settings) as db:
        if query.include_emails:
            require_unmask_permission(db, principal.session.role_at_login)
        content, media = export_results(
            db,
            None,
            query.format,
            filters,
            user_id=principal.user.user_id,
            now=request.app.state.clock(),
            include_emails=query.include_emails,
            sort=query.sort,
            descending=query.descending,
            limit=query.limit,
            offset=query.offset,
            run_pairs=current_runs(db),
        )
    return Response(
        content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="PFC-results-current.{query.format}"'
        },
    )


@router.get("/match-results/{result_id}", response_model=ResultDetail)
def detail(result_id: int, request: Request) -> ResultRow:
    with database(request.app.state.settings) as db:
        return result_view(db, required_result(db, result_id), detail=True)


@router.post("/match-results/{result_id}/overrides", response_model=ResultRow, status_code=201)
def override(
    result_id: int, body: OverrideRequest, request: Request, principal: Analyst
) -> ResultRow:
    with database(request.app.state.settings) as db:
        required_result(db, result_id)
        try:
            matching.add_override(
                db,
                result_id,
                body.action,
                body.new_band,
                body.reason,
                principal.user.user_id,
                request.app.state.clock(),
            )
        except ValueError as exc:
            raise Problem(409, "OVERRIDE_CONFLICT", str(exc)) from exc
        return result_view(db, required_result(db, result_id))


@router.delete("/match-results/{result_id}/overrides", response_model=ResultRow)
def revoke(
    result_id: int,
    request: Request,
    principal: Analyst,
    body: Annotated[OverrideReason | None, Body()] = None,
    reason: Annotated[str | None, Query()] = None,
    override_id: Annotated[int | None, Query(alias="overrideId", gt=0)] = None,
) -> ResultRow:
    explanation = body.reason if body is not None else reason
    if not explanation or not explanation.strip():
        raise Problem(422, "OVERRIDE_REASON_REQUIRED", "A nonblank reason is required")
    with database(request.app.state.settings) as db:
        db.execute("BEGIN IMMEDIATE")
        required_result(db, result_id)
        row = db.execute(
            "SELECT override_id FROM overrides WHERE result_id=? AND revoked_at IS NULL "
            "AND (? IS NULL OR override_id=?) ORDER BY override_id DESC LIMIT 1",
            (result_id, override_id, override_id),
        ).fetchone()
        if row is None:
            raise Problem(404, "OVERRIDE_NOT_FOUND", "Active override of this result not found")
        matching.revoke_override(
            db, row[0], explanation, principal.user.user_id, request.app.state.clock()
        )
        return result_view(db, required_result(db, result_id))


@router.get("/match-runs/{run_id}/excluded", response_model=list[ExcludedRow])
def excluded(run_id: int, request: Request) -> list[ExcludedRow]:
    with database(request.app.state.settings) as db:
        find_run(db, run_id)
        return [
            ExcludedRow(
                run_id=row["run_id"],
                resource_id=row["resource_id"],
                short_name=row["short_name"],
                demand_id=row["demand_id"],
                code_requisition=row["requisition"],
                reason_code=row["reason_code"],
                detail=row["detail"],
                scope="global" if row["demand_id"] is None else "pair",
            )
            for row in matching.excluded_view(db, run_id)
        ]
