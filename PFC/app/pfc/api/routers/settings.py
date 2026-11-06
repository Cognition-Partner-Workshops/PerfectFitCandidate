from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from pfc.adapters.llm.supervisor import restart_selected_model
from pfc.api.routers.occ import DB
from pfc.api.schemas.settings import PersonalSettings
from pfc.security.dependencies import Principal, current_user
from pfc.services.occ.settings import get_personal, put_personal

router = APIRouter(prefix="/settings", dependencies=[Depends(current_user)])
User = Annotated[Principal, Depends(current_user)]


@router.get("/me", response_model=PersonalSettings)
def get_settings(db: DB, user: User) -> PersonalSettings:
    """REQ-ING-04/05, REQ-IN-20: per-user data paths and display preferences."""
    return get_personal(db, user.user.user_id)


@router.put("/me", response_model=PersonalSettings)
def put_settings(
    body: PersonalSettings, db: DB, user: User, request: Request, background_tasks: BackgroundTasks
) -> PersonalSettings:
    """Commit preferences before scheduling a shared-server model switch."""
    result = put_personal(db, user.user.user_id, body.model_dump(exclude_unset=True))
    db.commit()
    if "active_model_id" in body.model_fields_set:
        background_tasks.add_task(
            restart_selected_model, request.app.state.settings, request.app.state
        )
    return result
