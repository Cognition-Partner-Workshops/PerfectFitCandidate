"""TDD §12 JD review and matching runs (Analyst and Admin)."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.schemas.match import (
    AnalysisFailure,
    BulkAnalysis,
    DemandIds,
    JdRequirements,
    RunAccepted,
    RunRequest,
    RunView,
    requirements_view,
    run_view,
)
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.services import matching

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


def find_demand(db: sqlite3.Connection, demand_id: int) -> None:
    if db.execute("SELECT 1 FROM demands WHERE demand_id=?", (demand_id,)).fetchone() is None:
        raise Problem(404, "DEMAND_NOT_FOUND", "Demand not found")


def available(request: Request) -> matching.MatchingPorts:
    ports = getattr(request.app.state, "matching_ports", None)
    if (
        ports is None
        or ports.embedder is None
        or getattr(request.app.state, "job_runner", None) is None
    ):
        raise Problem(503, "MATCHING_UNAVAILABLE", "Matching requires embedding ports and runner")
    return ports


def dispatch(request: Request, principal: Principal, body: RunRequest) -> RunAccepted:
    ports = available(request)
    try:
        run_id, job_id = matching.start_run(
            request.app.state.settings,
            request.app.state.job_runner,
            ports,
            scope=body.scope,
            demand_ids=body.demand_ids,
            as_of_date=body.as_of_date,
            user_id=principal.user.user_id,
            clock=request.app.state.clock,
        )
    except ValueError as exc:
        raise Problem(409, "MATCH_CONFIGURATION_CONFLICT", str(exc)) from exc
    except RuntimeError as exc:
        raise Problem(503, "MATCHING_UNAVAILABLE", "Matching runner is unavailable") from exc
    return RunAccepted(run_id=run_id, job_id=job_id)


@router.post("/demands/analyze", response_model=BulkAnalysis)
def analyze_bulk(body: DemandIds, request: Request, principal: Analyst) -> BulkAnalysis:
    analyzed, failed = [], []
    for demand_id in sorted(set(body.demand_ids)):
        try:
            analyzed.append(analyze(demand_id, request, principal))
        except Problem as exc:
            failed.append(AnalysisFailure(demand_id=demand_id, detail=str(exc.detail)))
    return BulkAnalysis(analyzed=analyzed, failed=failed)


@router.post("/demands/{demand_id}/analyze", response_model=JdRequirements)
def analyze(demand_id: int, request: Request, principal: Analyst) -> JdRequirements:
    ports = getattr(request.app.state, "matching_ports", None) or matching.MatchingPorts()
    with database(request.app.state.settings) as db:
        find_demand(db, demand_id)
        try:
            draft = matching.analyze_jd(
                db, demand_id, ports, principal.user.user_id, request.app.state.clock()
            )
        except ValueError as exc:
            raise Problem(409, "JD_ANALYSIS_CONFLICT", str(exc)) from exc
    return requirements_view(demand_id, draft)


@router.get("/demands/{demand_id}/requirements", response_model=JdRequirements)
def requirements(demand_id: int, request: Request) -> JdRequirements:
    with database(request.app.state.settings) as db:
        find_demand(db, demand_id)
        try:
            row = matching.get_requirements(db, demand_id)
        except ValueError as exc:
            raise Problem(409, "JD_VERSION_REQUIRED", str(exc)) from exc
    if row is None:
        raise Problem(404, "REQUIREMENTS_NOT_FOUND", "Requirements not found")
    return requirements_view(demand_id, row)


@router.put("/demands/{demand_id}/requirements", response_model=JdRequirements)
def edit(
    demand_id: int, body: JdRequirements, request: Request, principal: Analyst
) -> JdRequirements:
    if body.demand_id != demand_id:
        raise Problem(422, "DEMAND_ID_MISMATCH", "Body demandId must match the path")
    with database(request.app.state.settings) as db:
        find_demand(db, demand_id)
        try:
            row = matching.save_draft(
                db, demand_id, body.extraction(), principal.user.user_id, request.app.state.clock()
            )
        except ValueError as exc:
            raise Problem(409, "REQUIREMENTS_CONFLICT", str(exc)) from exc
    return requirements_view(demand_id, row)


@router.post(
    "/demands/{demand_id}/requirements/confirm", response_model=RunAccepted, status_code=202
)
def confirm(demand_id: int, request: Request, principal: Analyst) -> RunAccepted:
    available(request)
    with database(request.app.state.settings) as db:
        find_demand(db, demand_id)
        try:
            matching.confirm_requirements(
                db, demand_id, None, principal.user.user_id, request.app.state.clock()
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise Problem(409, "REQUIREMENTS_CONFLICT", "No draft requirements to confirm") from exc
    return dispatch(request, principal, RunRequest(scope="demands", demand_ids=[demand_id]))


@router.post("/match-runs", response_model=RunAccepted, status_code=202)
def start(body: RunRequest, request: Request, principal: Analyst) -> RunAccepted:
    available(request)
    with database(request.app.state.settings) as db:
        for demand_id in body.demand_ids:
            find_demand(db, demand_id)
    return dispatch(request, principal, body)


@router.get("/match-runs", response_model=list[RunView])
def runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunView]:
    with database(request.app.state.settings) as db:
        return [run_view(row) for row in matching.list_runs(db, limit=limit, offset=offset)]


@router.get("/match-runs/{run_id}", response_model=RunView)
def run(run_id: int, request: Request) -> RunView:
    with database(request.app.state.settings) as db:
        row = matching.get_run(db, run_id)
    if row is None:
        raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
    return run_view(row)
