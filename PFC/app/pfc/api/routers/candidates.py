"""REQ-IN-23..25: Analyst/Admin candidate identity workflow."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from pfc.api.schemas.candidates import (
    CandidateClassification,
    CandidateDetail,
    ClassificationOverride,
    IdentityResolution,
)
from pfc.api.schemas.summaries import SummaryView
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services import summaries
from pfc.services.candidates import classification
from pfc.services.ingestion import identity

router = APIRouter(prefix="/candidates", dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]
Admin = Annotated[Principal, Depends(require_role("Admin"))]


@router.post("/{candidate_id}/summary")
def generate_summary(
    candidate_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("Analyst"))],
) -> SummaryView:
    return summaries.generate(
        request.app.state.settings,
        getattr(request.app.state, "llm_provider", None),
        "candidate",
        candidate_id,
        principal.user.user_id,
        request.app.state.clock,
    )


@router.get("/{candidate_id}/summary")
def summary(candidate_id: int, request: Request) -> SummaryView:
    return summaries.latest(request.app.state.settings, "candidate", candidate_id)


@router.put("/{candidate_id}/classification")
def classify(
    candidate_id: int,
    body: ClassificationOverride,
    request: Request,
    principal: Admin,
) -> CandidateClassification:
    with database(request.app.state.settings) as db:
        db.execute("BEGIN IMMEDIATE")
        return classification.set_override(
            db,
            candidate_id,
            body,
            principal.user.user_id,
            stamp(request.app.state.clock()),
        )


@router.get("/{candidate_id}")
def candidate(candidate_id: int, request: Request) -> CandidateDetail:
    with database(request.app.state.settings) as db:
        return identity.detail(db, candidate_id)


@router.put("/{candidate_id}/identity")
def resolve(
    candidate_id: int, body: IdentityResolution, request: Request, principal: Analyst
) -> CandidateDetail:
    with database(request.app.state.settings) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            return identity.resolve(
                db, candidate_id, body, principal.user.user_id, stamp(request.app.state.clock())
            )
        except sqlite3.IntegrityError as exc:
            raise Problem(
                409, "IDENTITY_CONFLICT", "Identity conflicts with existing data"
            ) from exc
