import json
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from pfc.api.routers.occ import DB, Admin
from pfc.db.connection import database
from pfc.jobs.runner import JobContext
from pfc.security.dependencies import Principal, require_role
from pfc.services import archival, archive_browse, housekeeping

router = APIRouter()
User = Annotated[Principal, Depends(require_role("Analyst"))]


class Preview(BaseModel):
    ended_before: date | None = None


class Item(BaseModel):
    entity_type: Literal["demand", "resource"]
    entity_id: int = Field(gt=0)
    included: bool


class Items(BaseModel):
    items: list[Item]


class Run(BaseModel):
    preview_hash: str


@router.post("/occ/archive/preview")
def preview(body: Preview, request: Request, db: DB, admin: Admin) -> dict[str, Any]:
    return archival.preview(
        db,
        kind="archive",
        user_id=admin.user.user_id,
        now=request.app.state.clock(),
        ended_before=body.ended_before.isoformat() if body.ended_before else None,
    )


@router.post("/occ/archive/purge/preview")
def purge_preview(request: Request, db: DB, admin: Admin) -> dict[str, Any]:
    return archival.preview(
        db, kind="purge", user_id=admin.user.user_id, now=request.app.state.clock()
    )


@router.get("/occ/archive/batches/{batch_id}")
def batch(batch_id: int, db: DB, admin: Admin) -> dict[str, Any]:
    return archival.batch(db, batch_id)


@router.put("/occ/archive/batches/{batch_id}/items")
def items(batch_id: int, body: Items, db: DB, admin: Admin) -> dict[str, Any]:
    return archival.select_items(db, batch_id, [item.model_dump() for item in body.items])


@router.post("/occ/archive/batches/{batch_id}/run", status_code=202)
def run(batch_id: int, body: Run, request: Request, admin: Admin) -> dict[str, int]:
    return {
        "job_id": archival.submit(
            request.app.state.settings,
            request.app.state.job_runner,
            batch_id,
            body.preview_hash,
            admin.user.user_id,
            request.app.state.clock(),
            kind="archive",
        )
    }


@router.post("/occ/archive/purge/batches/{batch_id}/run", status_code=202)
def purge_run(batch_id: int, body: Run, request: Request, admin: Admin) -> dict[str, int]:
    return {
        "job_id": archival.submit(
            request.app.state.settings,
            request.app.state.job_runner,
            batch_id,
            body.preview_hash,
            admin.user.user_id,
            request.app.state.clock(),
            kind="purge",
        )
    }


@router.get("/archive/{collection}")
def search(
    collection: Literal["demands", "resources"],
    request: Request,
    user: User,
    q: str | None = None,
    grade: str | None = None,
    account: str | None = None,
    location: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    with database(request.app.state.settings) as db:
        return archive_browse.search(
            db,
            collection,
            q=q,
            grade=grade,
            account=account,
            location=location,
            page=page,
            page_size=page_size,
        )


@router.get("/archive/{collection}/export.csv")
def export(
    collection: Literal["demands", "resources"],
    request: Request,
    user: User,
    q: str | None = None,
    grade: str | None = None,
    account: str | None = None,
    location: str | None = None,
) -> Response:
    with database(request.app.state.settings) as db:
        result = archive_browse.export_csv(
            db,
            collection,
            user.user.user_id,
            request.app.state.clock().isoformat(),
            q=q,
            grade=grade,
            account=account,
            location=location,
        )
    return Response(
        result,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="archived-{collection}.csv"'},
    )


class Housekeeping(BaseModel):
    purge_temp: bool = False
    purge_logs: bool = False
    purge_orphan_vectors: bool = False
    vacuum: bool = False
    backup: bool = False


@router.get("/occ/housekeeping/preview")
def housekeeping_preview(request: Request, admin: Admin) -> dict[str, Any]:
    return housekeeping.preview(request.app.state.settings, request.app.state.clock())


@router.post("/occ/housekeeping", status_code=202)
def housekeeping_run(body: Housekeeping, request: Request, admin: Admin) -> dict[str, int]:
    def run(context: JobContext) -> None:
        counts = housekeeping.run(
            request.app.state.settings,
            body.model_dump(),
            admin.user.user_id,
            request.app.state.clock(),
        )
        context.report_progress(1, json.dumps(counts))
        context.raise_if_cancelled()
        context.runner._finish(context.job_id, "completed", json.dumps(counts))

    return {
        "job_id": request.app.state.job_runner.submit(
            "housekeeping", run, params=body.model_dump(), created_by=admin.user.user_id
        )
    }
