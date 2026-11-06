"""Frontend result rows and audited, non-destructive overrides."""

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from pfc.api.schemas.results import (
    BreakdownView,
    CvScoreView,
    ExcludedResource,
    OverrideReason,
    OverrideRequest,
    OverrideView,
    ResultDetail,
    ResultRow,
    ResultsQuery,
    band_label,
)
from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.services import matching
from pfc.services.matching.results import _mask_email

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


def find_run(db: sqlite3.Connection, run_id: int) -> None:
    if matching.get_run(db, run_id) is None:
        raise Problem(404, "RUN_NOT_FOUND", "Match run not found")


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
            "shortlisted",
        )
    }
    fields.update(
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
        fields[key] = [part.strip() for part in row[key].split("; ") if part.strip()]
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
    return row


@router.get("/match-runs/{run_id}/results", response_model=list[ResultRow])
def results(
    run_id: int, request: Request, response: Response, query: Annotated[ResultsQuery, Query()]
) -> list[ResultRow]:
    filters = query.model_dump(exclude={"sort", "descending", "limit", "offset"})
    with database(request.app.state.settings) as db:
        find_run(db, run_id)
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
        return [result_view(db, row) for row in page["items"]]


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


@router.get("/resources/excluded", response_model=list[ExcludedResource])
def excluded(
    request: Request, run_id: Annotated[int | None, Query(alias="runId", gt=0)] = None
) -> list[ExcludedResource]:
    with database(request.app.state.settings) as db:
        if run_id is not None:
            find_run(db, run_id)
        else:
            run_id = matching.list_results(db, limit=1)["run_id"]
        if run_id is None:
            return []
        exclusions = matching.excluded_view(db, run_id)
        reasons: dict[int, list[str]] = {}
        for row in exclusions:
            if row["demand_id"] is None:
                reasons.setdefault(row["resource_id"], []).append(
                    row["detail"] or row["reason_code"]
                )
        config = load_domain_config()
        output = []
        for resource_id, details in reasons.items():
            row = dict(
                db.execute(
                    "SELECT r.*,c.display_name,c.identity_status,"
                    "COALESCE(c.classification_override,c.classification,'Unknown') "
                    "AS candidate_type FROM resources r LEFT JOIN candidates c USING(candidate_id) "
                    "WHERE resource_id=?",
                    (resource_id,),
                ).fetchone()
            )
            status = config.bench_statuses.resolve(row["bench_status"] or "")
            cv_count = db.execute(
                "SELECT COUNT(*) FROM cv_versions WHERE candidate_id=?",
                (row["candidate_id"],),
            ).fetchone()[0]
            output.append(
                ExcludedResource(
                    resource_id=resource_id,
                    candidate_id=row["candidate_id"] or 0,
                    short_name=row["short_name"],
                    email_address_masked=_mask_email(row["email_address"]),
                    name=row["display_name"],
                    grade=row["grade"],
                    location=row["location"],
                    bench_status=row["bench_status"],
                    bench_status_priority=status.priority if status else None,
                    unmapped_status=status is None,
                    skills=[
                        value.strip()
                        for value in (row["skills_raw"] or "").split(",")
                        if value.strip()
                    ],
                    available_capacity=row["available_capacity"]
                    if row["available_capacity"] in {"Y", "N"}
                    else None,
                    bench_start_date=row["bench_start_date"],
                    bench_end_date=row["bench_end_date"],
                    group_name=row["group_name"],
                    group_owner=row["group_owner"],
                    exclusion_reasons=details,
                    cv_status="Synced" if cv_count else "Not synced",
                    cv_count=cv_count,
                    candidate_type=row["candidate_type"],
                    identity_status=row["identity_status"] or "incomplete",
                )
            )
        return output
