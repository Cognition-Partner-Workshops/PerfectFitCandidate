"""REQ-IN-21/24: Analyst/Admin CV jobs and metadata-only version history."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from pfc.api.schemas.cv import CVSyncJob, CVSyncRequest, CVVersionItem
from pfc.security.dependencies import Principal, require_role
from pfc.services.ingestion import cv_sync

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


def _submit(request: Request, principal: Principal, ids: list[int] | None) -> CVSyncJob:
    settings = request.app.state.settings
    user_id = principal.user.user_id
    root, selected = cv_sync.prepare(settings, user_id, ids)
    embedder = getattr(request.app.state, "embedder", None)
    job_id = request.app.state.job_runner.submit(
        "cv_sync",
        lambda context: cv_sync.sync(context, settings, root, selected, user_id, embedder),
        {"resource_ids": selected},
        created_by=user_id,
    )
    return CVSyncJob(job_id=job_id)


@router.post("/resources/cv-sync", status_code=202)
def sync_all(request: Request, principal: Analyst, body: CVSyncRequest | None = None) -> CVSyncJob:
    return _submit(request, principal, body.resource_ids if body else None)


@router.post("/resources/{resource_id}/cv-sync", status_code=202)
def sync_one(resource_id: int, request: Request, principal: Analyst) -> CVSyncJob:
    return _submit(request, principal, [resource_id])


@router.get("/candidates/{candidate_id}/cv-versions")
def versions(candidate_id: int, request: Request) -> list[CVVersionItem]:
    return cv_sync.versions(request.app.state.settings, candidate_id)
