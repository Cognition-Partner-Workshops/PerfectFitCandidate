from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from pfc.api.routers.occ import DB
from pfc.api.schemas.llm import ActiveModel, LlmHealth
from pfc.security.dependencies import Principal, current_user
from pfc.services.occ import llm
from pfc.services.occ.llm import LlmHealthProvider, UnconfiguredHealthProvider

router = APIRouter(prefix="/llm", dependencies=[Depends(current_user)])
User = Annotated[Principal, Depends(current_user)]


def health_provider(request: Request) -> LlmHealthProvider:
    return getattr(request.app.state, "llm_health_provider", UnconfiguredHealthProvider())


@router.get("/models")
def models(db: DB, user: User) -> list[dict[str, Any]]:
    """REQ-LLM-06: Analysts select only Admin-approved models."""
    return llm.list_models(db, user.user.user_id, approved_only=True)


@router.get("/active")
def active(db: DB, user: User) -> dict[str, Any] | None:
    return llm.active(db, user.user.user_id)


@router.put("/active")
def select(body: ActiveModel, db: DB, user: User) -> dict[str, Any] | None:
    """REQ-LLM-01/06: a per-user model choice never changes global model state."""
    return llm.select(db, user.user.user_id, body.model_id)


@router.get("/health", response_model=LlmHealth)
def health(provider: Annotated[LlmHealthProvider, Depends(health_provider)]) -> LlmHealth:
    return provider.check()
