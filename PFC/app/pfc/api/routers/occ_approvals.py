"""REQ-ROLE-03: Admin-only DL/CT approval queue."""

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from pfc.api.schemas.occ_approvals import ApprovalDecision, ApprovalEntry
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services.ingestion import approvals

router = APIRouter(prefix="/occ", dependencies=[Depends(require_role("Admin"))])
Admin = Annotated[Principal, Depends(require_role("Admin"))]


def connection(request: Request) -> Iterator[sqlite3.Connection]:
    with database(request.app.state.settings) as db:
        try:
            if request.method != "GET":
                db.execute("BEGIN IMMEDIATE")
            yield db
        except sqlite3.IntegrityError as exc:
            raise Problem(409, "CONFLICT", "Decision conflicts with existing data") from exc


DB = Annotated[sqlite3.Connection, Depends(connection)]


@router.get("/approvals", response_model_exclude_unset=True)
def queue(db: DB) -> list[ApprovalEntry]:
    return approvals.list_entries(db)


@router.get("/demands/{entity_id}", response_model_exclude_unset=True)
def demand_entry(entity_id: int, db: DB) -> ApprovalEntry:
    return approvals.entry(db, approvals.latest(db, "demand", entity_id))


@router.get("/resources/{entity_id}", response_model_exclude_unset=True)
def resource_entry(entity_id: int, db: DB) -> ApprovalEntry:
    return approvals.entry(db, approvals.latest(db, "resource", entity_id))


@router.put("/demands/{entity_id}", response_model_exclude_unset=True)
def decide_demand(
    entity_id: int, body: ApprovalDecision, request: Request, db: DB, admin: Admin
) -> ApprovalEntry:
    return approvals.decide(
        db, "demand", entity_id, body, admin.user.user_id, stamp(request.app.state.clock())
    )


@router.put("/resources/{entity_id}", response_model_exclude_unset=True)
def decide_resource(
    entity_id: int, body: ApprovalDecision, request: Request, db: DB, admin: Admin
) -> ApprovalEntry:
    return approvals.decide(
        db, "resource", entity_id, body, admin.user.user_id, stamp(request.app.state.clock())
    )
