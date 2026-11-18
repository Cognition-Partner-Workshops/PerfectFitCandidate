"""REQ-IN-21/24: Analyst/Admin CV jobs, metadata-only version history and comparison."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.routers.match import available
from pfc.api.schemas.cv import CVSyncJob, CVSyncRequest, CVVersionItem
from pfc.api.schemas.cv_compare import (
    CvCompareJob,
    CvCompareRequest,
    CvCompareScoring,
    CvComparison,
)
from pfc.security.dependencies import Principal, require_role
from pfc.services.candidates import cv_compare
from pfc.services.ingestion import cv_sync
from pfc.services.ingestion.cv_facts import LLMBridge

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


async def _submit(request: Request, principal: Principal, ids: list[int] | None) -> CVSyncJob:
    settings = request.app.state.settings
    user_id = principal.user.user_id
    root, selected, extensions = await asyncio.to_thread(cv_sync.prepare, settings, user_id, ids)
    embedder = getattr(request.app.state, "embedder", None)
    provider = getattr(request.app.state, "llm_provider", None)
    if not hasattr(request.app.state, "cv_facts_limiter"):
        request.app.state.cv_facts_limiter = asyncio.Semaphore(1)
    bridge = (
        LLMBridge(provider, asyncio.get_running_loop(), request.app.state.cv_facts_limiter)
        if provider is not None
        else None
    )
    job_id = request.app.state.job_runner.submit(
        "cv_sync",
        lambda context: cv_sync.sync(
            context, settings, root, selected, user_id, embedder, bridge, extensions=extensions
        ),
        {"resource_ids": selected},
        created_by=user_id,
    )
    return CVSyncJob(job_id=job_id)


@router.post("/resources/cv-sync", status_code=202)
async def sync_all(
    request: Request, principal: Analyst, body: CVSyncRequest | None = None
) -> CVSyncJob:
    return await _submit(request, principal, body.resource_ids if body else None)


@router.post("/resources/{resource_id}/cv-sync", status_code=202)
async def sync_one(resource_id: int, request: Request, principal: Analyst) -> CVSyncJob:
    return await _submit(request, principal, [resource_id])


@router.get("/candidates/{candidate_id}/cv-versions")
def versions(candidate_id: int, request: Request) -> list[CVVersionItem]:
    return cv_sync.versions(request.app.state.settings, candidate_id)


Version = Annotated[int, Query(ge=1)]


@router.get("/candidates/{candidate_id}/cv-versions/compare")
def compare(candidate_id: int, left: Version, right: Version, request: Request) -> CvComparison:
    """FR-ING-52: read-only, masked facts, text diff and stored scores for two versions."""
    return CvComparison.model_validate(
        cv_compare.compare(request.app.state.settings, candidate_id, left, right)
    )


@router.post("/candidates/{candidate_id}/cv-comparisons", status_code=202)
def score_comparison(
    candidate_id: int, body: CvCompareRequest, request: Request, principal: Analyst
) -> CvCompareJob:
    """Queue what-if scoring of both versions for current demands; results never change."""
    comparison_id, job_id = cv_compare.start_scoring(
        request.app.state.settings,
        request.app.state.job_runner,
        available(request),
        candidate_id,
        body.left_cv_version_id,
        body.right_cv_version_id,
        user_id=principal.user.user_id,
        clock=request.app.state.clock,
    )
    return CvCompareJob(comparison_id=comparison_id, job_id=job_id)


@router.get("/candidates/{candidate_id}/cv-comparisons/latest")
def latest_comparison(
    candidate_id: int, left: Version, right: Version, request: Request
) -> CvCompareScoring | None:
    found = cv_compare.latest_scoring(request.app.state.settings, candidate_id, left, right)
    return CvCompareScoring.model_validate(found) if found else None
