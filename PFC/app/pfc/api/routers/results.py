"""Masked matching results and audited analyst actions (REQ-OUT-01/03, REQ-MAT-09)."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from pfc.api.schemas.results import (
    DemandMatches,
    ExcludedRow,
    OverrideReason,
    OverrideRequest,
    OverrideView,
    ResultDetail,
    ResultsPage,
    ResultsQuery,
)
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.services import matching

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


def find_run(db: sqlite3.Connection, run_id: int) -> None:
    if matching.get_run(db, run_id) is None:
        raise Problem(404, "RUN_NOT_FOUND", "Match run not found")


@router.get("/results", response_model=ResultsPage)
def results(request: Request, query: Annotated[ResultsQuery, Query()]) -> ResultsPage:
    filters = query.model_dump(exclude={"run_id", "sort", "descending", "limit", "offset"})
    with database(request.app.state.settings) as db:
        if query.run_id is not None:
            find_run(db, query.run_id)
        page = matching.list_results(
            db,
            query.run_id,
            filters,
            sort=query.sort,
            descending=query.descending,
            limit=query.limit,
            offset=query.offset,
        )
    return ResultsPage.model_validate(page)


@router.get("/results/excluded", response_model=list[ExcludedRow])
def excluded(
    request: Request,
    run_id: Annotated[int | None, Query(gt=0)] = None,
) -> list[ExcludedRow]:
    with database(request.app.state.settings) as db:
        if run_id is not None:
            find_run(db, run_id)
        else:
            run_id = matching.list_results(db, limit=1)["run_id"]
        rows = matching.excluded_view(db, run_id) if run_id is not None else []
    return [ExcludedRow.model_validate(row) for row in rows]


@router.get("/results/{result_id}", response_model=ResultDetail)
def detail(result_id: int, request: Request) -> ResultDetail:
    with database(request.app.state.settings) as db:
        result = matching.result_detail(db, result_id)
    if result is None:
        raise Problem(404, "RESULT_NOT_FOUND", "Match result not found")
    return ResultDetail.model_validate(result)


@router.post("/results/{result_id}/overrides", response_model=OverrideView, status_code=201)
def override(
    result_id: int,
    body: OverrideRequest,
    request: Request,
    principal: Analyst,
) -> OverrideView:
    with database(request.app.state.settings) as db:
        if (
            db.execute("SELECT 1 FROM match_results WHERE result_id=?", (result_id,)).fetchone()
            is None
        ):
            raise Problem(404, "RESULT_NOT_FOUND", "Match result not found")
        try:
            override_id = matching.add_override(
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
        row = db.execute("SELECT * FROM overrides WHERE override_id=?", (override_id,)).fetchone()
    return OverrideView.model_validate(dict(row))


@router.delete("/results/overrides/{override_id}", status_code=204)
def revoke(
    override_id: int,
    body: OverrideReason,
    request: Request,
    principal: Analyst,
) -> Response:
    with database(request.app.state.settings) as db:
        if (
            db.execute("SELECT 1 FROM overrides WHERE override_id=?", (override_id,)).fetchone()
            is None
        ):
            raise Problem(404, "OVERRIDE_NOT_FOUND", "Override not found")
        try:
            matching.revoke_override(
                db,
                override_id,
                body.reason,
                principal.user.user_id,
                request.app.state.clock(),
            )
        except ValueError as exc:
            raise Problem(409, "OVERRIDE_CONFLICT", str(exc)) from exc
    return Response(status_code=204)


@router.get("/demands/{demand_id}/matches", response_model=DemandMatches)
def demand_results(demand_id: int, request: Request) -> DemandMatches:
    with database(request.app.state.settings) as db:
        if db.execute("SELECT 1 FROM demands WHERE demand_id=?", (demand_id,)).fetchone() is None:
            raise Problem(404, "DEMAND_NOT_FOUND", "Demand not found")
        result = matching.demand_matches(db, demand_id)
    return DemandMatches.model_validate(result)
