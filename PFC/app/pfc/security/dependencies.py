from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from pfc.db.sessions import Session
from pfc.db.users import User
from pfc.security.problems import Problem


@dataclass(frozen=True)
class Principal:
    user: User
    session: Session


def current_user(request: Request) -> Principal:
    """REQ-AUTH-01/02, REQ-ROLE-03/04: server-side session identity."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise Problem(401, "AUTH_REQUIRED", "Authentication required")
    return principal


def require_role(role: str) -> Callable[[Request], Principal]:
    canonical = role.capitalize()
    if canonical not in {"Admin", "Analyst"}:
        raise ValueError("Unknown role")

    def dependency(request: Request) -> Principal:
        principal = current_user(request)
        if canonical == "Admin" and principal.session.role_at_login != "Admin":
            raise Problem(403, "FORBIDDEN", "Insufficient permissions")
        return principal

    return dependency
