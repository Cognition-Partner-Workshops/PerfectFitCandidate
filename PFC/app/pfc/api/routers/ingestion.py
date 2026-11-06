"""REQ-ING-01..03/06: authenticated manual workbook uploads and run review."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from pfc.api.schemas.ingestion import IngestionRun, IssuePage, RunPage
from pfc.config import Settings
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services.embeddings.chunking import OwnerType
from pfc.services.embeddings.incremental import submit_incremental_embed
from pfc.services.ingestion import repository, service

logger = logging.getLogger(__name__)

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
        data = await file.read(service.MAX_UPLOAD_BYTES + 1)
        if len(data) > service.MAX_UPLOAD_BYTES:
            raise Problem(413, "UPLOAD_TOO_LARGE", "Workbook exceeds 20 MB limit")
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
    before = None
    try:
        with database(request.app.state.settings) as db:
            if repository.find_run(db, run_id)["kind"] == "DL":
                before = _current_versions(request.app.state.settings)
    except Exception as exc:
        logger.warning("Cannot snapshot demands for incremental embedding: %s", exc)
    result = service.commit(
        request.app.state.settings, run_id, principal.user.user_id, stamp(request.app.state.clock())
    )
    # CT skills are not chunked yet, so CT commits produce no embedding owners.
    if result.kind == "DL" and before is not None:
        try:
            after = _current_versions(request.app.state.settings)
            owners: set[tuple[OwnerType, int]] = set()
            for demand_id, current in after.items():
                previous = before.get(demand_id)
                if previous != current:
                    version = current[0]
                    old_version = previous[0] if previous is not None else None
                    for id in (old_version, version):
                        if id is not None:
                            owners.add(("jd", id))
                    if old_version is not None and old_version != version:
                        owners.update(
                            ("jd_item", id)
                            for id in _indexed_requirement_items(
                                request.app.state.settings, old_version
                            )
                        )
                    if version is not None and old_version != version:
                        owners.update(
                            ("jd_item", id)
                            for id in _confirmed_requirement_items(
                                request.app.state.settings, version
                            )
                        )
            if (
                owners
                and submit_incremental_embed(
                    request.app.state.job_runner,
                    request.app.state.settings,
                    sorted(owners),
                    created_by=principal.user.user_id,
                )
                is None
            ):
                logger.warning("JD embedding not queued: no active embedding model")
        except Exception as exc:
            logger.warning("JD embedding submission failed after ingestion commit: %s", exc)
    return result


def _current_versions(settings: Settings) -> dict[int, tuple[int | None, str | None]]:
    with database(settings) as db:
        return {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT demand_id,current_version_id,demand_request_name FROM demands"
            )
        }


def _confirmed_requirement_items(settings: Settings, version_id: int) -> set[int]:
    with database(settings) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT i.item_id FROM jd_requirement_items i "
                "JOIN jd_requirements r ON r.jd_req_id=i.jd_req_id "
                "WHERE r.demand_version_id=? AND r.status='confirmed'",
                (version_id,),
            )
        }


def _indexed_requirement_items(settings: Settings, version_id: int) -> set[int]:
    with database(settings) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT i.item_id FROM jd_requirement_items i "
                "JOIN jd_requirements r ON r.jd_req_id=i.jd_req_id "
                "JOIN embedding_chunks c ON c.owner_type='jd_item' AND c.owner_id=i.item_id "
                "JOIN embedding_models m ON m.embedding_model_id=c.embedding_model_id "
                "WHERE r.demand_version_id=? AND m.is_active=1",
                (version_id,),
            )
        }


@router.post("/ingestion-runs/{run_id}/discard")
def discard(run_id: int, request: Request, principal: Analyst) -> IngestionRun:
    return service.discard(
        request.app.state.settings, run_id, principal.user.user_id, stamp(request.app.state.clock())
    )
