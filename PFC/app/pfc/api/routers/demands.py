"""REQ-OUT-01: Analyst/Admin demand browsing."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.schemas.demands import DemandDetail, DemandPage, DemandQuery
from pfc.security.dependencies import require_role
from pfc.services.ingestion import browse

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])


@router.get("/demands")
def demands(request: Request, query: Annotated[DemandQuery, Query()]) -> DemandPage:
    return browse.demands(
        request.app.state.settings,
        query.page,
        query.page_size,
        {
            "bu": query.bu,
            "region": query.region,
            "account_name": query.account_name,
            "code_local_grade": query.grade,
            "lifecycle": query.lifecycle,
        },
        query.q,
    )


@router.get("/demands/{demand_id}")
def demand_detail(demand_id: int, request: Request) -> DemandDetail:
    return browse.demand_detail(request.app.state.settings, demand_id)
