from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from pfc.api.routers.occ_approvals import DB
from pfc.security.dependencies import Principal, require_role
from pfc.security.sessions import stamp
from pfc.services import lifecycle

router = APIRouter(prefix="/lifecycle", dependencies=[Depends(require_role("Analyst"))])
User = Annotated[Principal, Depends(require_role("Analyst"))]


@router.get("/reasons")
def reasons() -> dict[str, list[str]]:
    return {outcome: sorted(codes) for outcome, codes in lifecycle.REASONS.items()}


class Change(BaseModel):
    outcome: Literal["filled", "cancelled", "closed", "off_bench", "released"]
    reason_code: str
    comment: str | None = Field(default=None, max_length=500)
    expected_updated_at: str
    expected_event_id: int = Field(ge=0)


@router.post("/{entity_type}/{entity_id}/end")
def end(
    entity_type: Literal["demand", "resource"],
    entity_id: int,
    body: Change,
    db: DB,
    user: User,
    request: Request,
) -> dict[str, Any]:
    return lifecycle.manual_change(
        db,
        entity_type,
        entity_id,
        **body.model_dump(),
        user_id=user.user.user_id,
        now=stamp(request.app.state.clock()),
    )


class Reopen(BaseModel):
    reason_code: str
    comment: str | None = Field(default=None, max_length=500)
    expected_updated_at: str
    expected_event_id: int = Field(ge=0)


@router.post("/{entity_type}/{entity_id}/reopen")
def reopen(
    entity_type: Literal["demand", "resource"],
    entity_id: int,
    body: Reopen,
    db: DB,
    user: User,
    request: Request,
) -> dict[str, Any]:
    return lifecycle.manual_change(
        db,
        entity_type,
        entity_id,
        outcome="reopen",
        **body.model_dump(),
        user_id=user.user.user_id,
        now=stamp(request.app.state.clock()),
    )


@router.get("/events")
def events(
    db: DB,
    entity_type: Literal["demand", "resource"] | None = None,
    entity_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return lifecycle.events(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
