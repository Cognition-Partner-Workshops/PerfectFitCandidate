from typing import Annotated

from fastapi import APIRouter, Depends

from pfc.api.routers.occ import DB
from pfc.api.schemas.pii import UnmaskRequest, UnmaskResponse
from pfc.security.dependencies import Principal, require_role
from pfc.services.pii import unmask

router = APIRouter(prefix="/pii")
User = Annotated[Principal, Depends(require_role("Analyst"))]


@router.post("/unmask", response_model=UnmaskResponse)
def unmask_field(body: UnmaskRequest, db: DB, user: User) -> UnmaskResponse:
    """REQ-OUT-03, REQ-AUTH-05: reveal exactly one field and audit without its value."""
    return UnmaskResponse(
        value=unmask(
            db, user.user.user_id, user.session.role_at_login, body.entity, body.id, body.field
        )
    )
