"""REQ-OUT-01: Analyst/Admin demand browsing."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.schemas.demands import DemandDetail, DemandPage, DemandQuery
from pfc.api.schemas.summaries import SummaryView
from pfc.security.dependencies import Principal, require_role
from pfc.services import summaries
from pfc.services.ingestion import browse

router = APIRouter(dependencies=[Depends(require_role("Analyst"))])


@router.post("/demands/{demand_id}/summary")
def generate_summary(
    demand_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("Analyst"))],
) -> SummaryView:
    return summaries.generate(
        request.app.state.settings,
        getattr(request.app.state, "llm_provider", None),
        "demand",
        demand_id,
        principal.user.user_id,
        request.app.state.clock,
    )


@router.get("/demands/{demand_id}/summary")
def summary(demand_id: int, request: Request) -> SummaryView:
    return summaries.latest(request.app.state.settings, "demand", demand_id)


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
