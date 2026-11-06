"""Authenticated JD review and run dispatch (REQ-MAT-02/03/08)."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.schemas.match import (
    RequirementsView,
    RunAccepted,
    RunRequest,
    RunView,
    extraction_payload,
)
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.services import matching

router = APIRouter(prefix="/match", dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


def find_demand(db: sqlite3.Connection, demand_id: int) -> None:
    if db.execute("SELECT 1 FROM demands WHERE demand_id=?", (demand_id,)).fetchone() is None:
        raise Problem(404, "DEMAND_NOT_FOUND", "Demand not found")


@router.post("/analyze-jd/{demand_id}", response_model=RequirementsView)
def analyze(demand_id: int, request: Request, principal: Analyst) -> RequirementsView:
    ports = getattr(request.app.state, "matching_ports", None) or matching.MatchingPorts(None)
    with database(request.app.state.settings) as db:
        find_demand(db, demand_id)
        try:
            draft = matching.analyze_jd(
                db,
                demand_id,
                ports,
                principal.user.user_id,
                request.app.state.clock(),
            )
        except ValueError as exc:
            raise Problem(409, "JD_ANALYSIS_CONFLICT", str(exc)) from exc
    return RequirementsView.model_validate(draft)


@router.get("/requirements/{demand_id}", response_model=RequirementsView)
def requirements(demand_id: int, request: Request) -> RequirementsView:
    with database(request.app.state.settings) as db:
        find_demand(db, demand_id)
        try:
            result = matching.get_requirements(db, demand_id)
        except ValueError as exc:
            raise Problem(409, "JD_VERSION_REQUIRED", str(exc)) from exc
    if result is None:
        raise Problem(404, "REQUIREMENTS_NOT_FOUND", "Requirements not found")
    return RequirementsView.model_validate(result)


@router.post("/runs", response_model=RunAccepted, status_code=202)
def start(body: RunRequest, request: Request, principal: Analyst) -> RunAccepted:
    ports = getattr(request.app.state, "matching_ports", None)
    runner = getattr(request.app.state, "job_runner", None)
    if ports is None or ports.embedder is None or runner is None:
        raise Problem(
            503, "MATCHING_UNAVAILABLE", "Matching requires configured embedding ports and runner"
        )
    with database(request.app.state.settings) as db:
        db.execute("BEGIN IMMEDIATE")
        for demand_id in sorted(set(body.demand_ids) | set(body.edited_requirements)):
            find_demand(db, demand_id)
        try:
            for demand_id, edits in sorted(body.edited_requirements.items()):
                matching.confirm_requirements(
                    db,
                    demand_id,
                    extraction_payload(edits),
                    principal.user.user_id,
                    request.app.state.clock(),
                )
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise Problem(409, "REQUIREMENTS_CONFLICT", "Requirements cannot be confirmed") from exc
    try:
        run_id, job_id = matching.start_run(
            request.app.state.settings,
            runner,
            ports,
            scope=body.scope,
            demand_ids=body.demand_ids,
            user_id=principal.user.user_id,
            as_of_date=body.as_of_date,
            clock=request.app.state.clock,
        )
    except ValueError as exc:
        raise Problem(409, "MATCH_CONFIGURATION_CONFLICT", str(exc)) from exc
    except RuntimeError as exc:
        raise Problem(503, "MATCHING_UNAVAILABLE", "Matching runner is unavailable") from exc
    return RunAccepted(run_id=run_id, job_id=job_id)


@router.get("/runs", response_model=list[RunView])
def runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunView]:
    with database(request.app.state.settings) as db:
        return [
            RunView.model_validate(row)
            for row in matching.list_runs(db, limit=limit, offset=offset)
        ]


@router.get("/runs/{run_id}", response_model=RunView)
def run(run_id: int, request: Request) -> RunView:
    with database(request.app.state.settings) as db:
        result = matching.get_run(db, run_id)
    if result is None:
        raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
    return RunView.model_validate(result)
