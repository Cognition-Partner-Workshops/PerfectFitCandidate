"""REQ-ING-01..03/06: authenticated manual workbook uploads and run review."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from pfc.api.schemas.ingestion import IngestionRun, IssuePage, RunPage
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.sessions import stamp
from pfc.services.ingestion import repository, service

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.post("/uploads/{kind}", response_model=IngestionRun)
async def upload(
    kind: Literal["dl", "ct"],
    request: Request,
    principal: Analyst,
    file: Annotated[UploadFile, File()],
) -> IngestionRun:
    try:
        data = await file.read(service.upload_limit(request.app.state.settings) + 1)
        return service.upload(
            request.app.state.settings,
            "DL" if kind == "dl" else "CT",
            file.filename or "",
            data,
            principal.user.user_id,
            stamp(request.app.state.clock()),
        )
    finally:
        await file.close()


@router.get("/ingestion-runs")
def history(request: Request, limit: Limit = 25, offset: Offset = 0) -> RunPage:
    return service.history(request.app.state.settings, limit, offset)


@router.get("/ingestion-runs/{run_id}")
def get_run(run_id: int, request: Request) -> IngestionRun:
    with database(request.app.state.settings) as db:
        return repository.run_response(db, request.app.state.settings, run_id)


@router.get("/ingestion-runs/{run_id}/issues")
def issues(run_id: int, request: Request, limit: Limit = 100, offset: Offset = 0) -> IssuePage:
    with database(request.app.state.settings) as db:
        return repository.issues(db, run_id, limit, offset)


@router.post("/ingestion-runs/{run_id}/commit")
def commit(run_id: int, request: Request, principal: Analyst) -> IngestionRun:
    return service.commit(
        request.app.state.settings, run_id, principal.user.user_id, stamp(request.app.state.clock())
    )


@router.post("/ingestion-runs/{run_id}/discard")
def discard(run_id: int, request: Request, principal: Analyst) -> IngestionRun:
    return service.discard(
        request.app.state.settings, run_id, principal.user.user_id, stamp(request.app.state.clock())
    )
