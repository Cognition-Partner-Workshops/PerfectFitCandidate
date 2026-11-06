"""REQ-OUT-03 / REQ-ELIG-08: masked Analyst/Admin resource browsing."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.schemas.resources import ResourceDetail, ResourcePage, ResourceQuery
from pfc.security.dependencies import require_role
from pfc.services.ingestion import browse

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])


def _list(request: Request, query: ResourceQuery, excluded: bool = False) -> ResourcePage:
    return browse.resources(
        request.app.state.settings,
        query.page,
        query.page_size,
        {
            "grade": query.grade,
            "bench_status_code": query.bench_status_code,
            "eligibility": "excluded" if excluded else query.eligibility,
            "lifecycle": query.lifecycle,
            "group_name": query.group_name,
        },
        query.location,
        query.q,
    )


@router.get("/resources")
def resources(request: Request, query: Annotated[ResourceQuery, Query()]) -> ResourcePage:
    return _list(request, query)


@router.get("/resources/excluded")
def excluded(request: Request, query: Annotated[ResourceQuery, Query()]) -> ResourcePage:
    return _list(request, query, excluded=True)


@router.get("/resources/{resource_id}")
def resource_detail(resource_id: int, request: Request) -> ResourceDetail:
    return browse.resource_detail(request.app.state.settings, resource_id)
