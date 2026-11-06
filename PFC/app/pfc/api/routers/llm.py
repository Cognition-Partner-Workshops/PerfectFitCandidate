from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from pfc.api.routers.occ import DB
from pfc.api.schemas.llm import ActiveModel, LlmHealth
from pfc.db.connection import database
from pfc.security.dependencies import Principal, current_user
from pfc.services.occ import llm
from pfc.services.occ.llm import LlmHealthProvider, UnconfiguredHealthProvider

router = APIRouter(prefix="/llm", dependencies=[Depends(current_user)])
User = Annotated[Principal, Depends(current_user)]


def health_provider(request: Request) -> LlmHealthProvider:
    return getattr(request.app.state, "llm_health_provider", UnconfiguredHealthProvider())


async def restart_selected_model(request: Request) -> None:
    supervisor = getattr(request.app.state, "llm_supervisor", None)
    if supervisor is not None:
        with database(request.app.state.settings) as db:
            model = supervisor.resolve_model(db)
        supervisor.schedule(model)


@router.get("/models")
def models(db: DB, user: User) -> list[dict[str, Any]]:
    """REQ-LLM-06: Analysts select only Admin-approved models."""
    return llm.list_models(db, user.user.user_id, approved_only=True)


@router.get("/active")
def active(db: DB, user: User) -> dict[str, Any] | None:
    return llm.active(db, user.user.user_id)


@router.put("/active")
def select(
    body: ActiveModel, db: DB, user: User, request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any] | None:
    """REQ-LLM-01/06: commit the choice, then asynchronously switch the shared local server."""
    result = llm.select(db, user.user.user_id, body.model_id)
    db.commit()
    background_tasks.add_task(restart_selected_model, request)
    return result


@router.get("/health", response_model=LlmHealth)
def health(provider: Annotated[LlmHealthProvider, Depends(health_provider)]) -> LlmHealth:
    return provider.check()
