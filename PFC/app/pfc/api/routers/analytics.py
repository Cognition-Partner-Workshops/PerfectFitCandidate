"""Dashboard demand/person filters are independent of result-only filters."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pfc.api.schemas.analytics import (
    AnalyticsSummary,
    BenchAnalytics,
    DashboardFilters,
    DemandAnalytics,
    MatchAnalytics,
)
from pfc.db.connection import database
from pfc.security.dependencies import require_role
from pfc.services.analytics import dashboard
from pfc.services.analytics.population import Population

router = APIRouter(prefix="/analytics", dependencies=[Depends(require_role("Analyst"))])
Filters = Annotated[DashboardFilters, Query()]


@router.get("/summary", response_model=AnalyticsSummary)
def summary(request: Request, filters: Filters) -> AnalyticsSummary:
    with database(request.app.state.settings) as db:
        population = Population(db, filters, request.app.state.clock().date())
        return AnalyticsSummary(**dashboard.summary(population))


@router.get("/demand", response_model=DemandAnalytics)
def demand(request: Request, filters: Filters) -> DemandAnalytics:
    with database(request.app.state.settings) as db:
        population = Population(db, filters, request.app.state.clock().date())
        return DemandAnalytics(**dashboard.demand(population))


@router.get("/bench", response_model=BenchAnalytics)
def bench(request: Request, filters: Filters) -> BenchAnalytics:
    with database(request.app.state.settings) as db:
        population = Population(db, filters, request.app.state.clock().date())
        return BenchAnalytics(**dashboard.bench(population))


@router.get("/match", response_model=MatchAnalytics)
def match(request: Request, filters: Filters) -> MatchAnalytics:
    with database(request.app.state.settings) as db:
        population = Population(db, filters, request.app.state.clock().date())
        return MatchAnalytics(**dashboard.match(population))
