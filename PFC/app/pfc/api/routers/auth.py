import sqlite3
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from pfc.api.schemas.auth import AuthView, ChangePassword, Login, SetupAdmin, UserView
from pfc.db import audit, sessions, users
from pfc.db.connection import database
from pfc.security.dependencies import Principal, current_user
from pfc.security.passwords import hasher, validate_password, verify
from pfc.security.problems import Problem
from pfc.security.sessions import csrf_token, issue, parse, stamp
from pfc.security.setup import consume_setup_code, verify_setup_code

router = APIRouter()
Authenticated = Annotated[Principal, Depends(current_user)]


def view(db: sqlite3.Connection, user: users.User, role: str, csrf: str) -> AuthView:
    assigned = db.execute("SELECT name FROM roles WHERE role_id=?", (user.role_id,)).fetchone()[0]
    return AuthView(
        user=UserView(
            user_id=user.user_id,
            email=user.email,
            display_name=user.display_name,
            role=assigned,
            must_change_password=bool(user.must_change_password),
        ),
        role_at_login=role,
        must_change_password=bool(user.must_change_password),
        csrf_token=csrf,
    )


def cookie(response: Response, token: str) -> None:
    response.set_cookie("pfc_session", token, httponly=True, samesite="strict", path="/")


@router.post("/setup/admin", response_model=UserView, status_code=201)
def setup_admin(body: SetupAdmin, request: Request) -> UserView:
    """REQ-AUTH-02/03/05: one-time first administrator bootstrap."""
    settings = request.app.state.settings
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        if users.count_admins(db):
            raise Problem(409, "ADMIN_EXISTS", "Administrator already exists")
        if not verify_setup_code(settings, body.setup_code):
            raise Problem(403, "SETUP_CODE_INVALID", "Invalid setup code")
        validate_password(body.password, body.email, "Admin")
        now = stamp(request.app.state.clock())
        role_id = db.execute("SELECT role_id FROM roles WHERE name='Admin'").fetchone()[0]
        user = users.create_user(
            db,
            email=body.email,
            display_name=body.display_name,
            role_id=role_id,
            password_hash=hasher(settings).hash(body.password),
            now=now,
            must_change_password=False,
        )
        audit.append_audit(db, ts=now, user_id=user.user_id, action="SETUP_ADMIN")
        db.commit()
        consume_setup_code(settings)
        return view(db, user, "Admin", "").user


@router.post("/auth/login", response_model=AuthView)
def login(body: Login, request: Request, response: Response) -> AuthView:
    """REQ-AUTH-01/04/05: role-aware login, lockout and audited generic failures."""
    settings = request.app.state.settings
    now = request.app.state.clock()
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        user = users.get_user_by_email(db, body.email)
        encoded = user.password_hash if user else request.app.state.dummy_hash
        password_ok = verify(encoded, body.password, settings)
        assigned = (
            db.execute("SELECT name FROM roles WHERE role_id=?", (user.role_id,)).fetchone()[0]
            if user
            else None
        )
        locked = bool(user and user.locked_until and parse(user.locked_until) > now)
        valid = (
            user
            and user.is_active
            and password_ok
            and not locked
            and (body.role == assigned or assigned == "Admin")
        )
        if not valid:
            audit.append_audit(
                db, ts=stamp(now), user_id=user.user_id if user else None, action="LOGIN_FAIL"
            )
            if user and not locked:
                failures = user.failed_attempts + 1
                if user.locked_until and parse(user.locked_until) <= now:
                    failures = 1
                until = None
                if failures >= settings.lockout_threshold:
                    until = stamp(now + timedelta(minutes=settings.lockout_minutes))
                    audit.append_audit(db, ts=stamp(now), user_id=user.user_id, action="LOCKOUT")
                users.update_login_state(
                    db, user.user_id, failed_attempts=failures, locked_until=until, now=stamp(now)
                )
            db.commit()
            raise Problem(401, "INVALID_CREDENTIALS", "invalid credentials")
        assert user is not None
        users.update_login_state(
            db, user.user_id, failed_attempts=0, locked_until=None, now=stamp(now)
        )
        if hasher(settings).check_needs_rehash(user.password_hash):
            users.update_password(
                db,
                user.user_id,
                password_hash=hasher(settings).hash(body.password),
                must_change_password=bool(user.must_change_password),
                now=stamp(now),
            )
        db.execute("UPDATE users SET last_login_at=? WHERE user_id=?", (stamp(now), user.user_id))
        token, csrf = issue(db, settings, user.user_id, body.role, now)
        audit.append_audit(db, ts=stamp(now), user_id=user.user_id, action="LOGIN_OK")
        cookie(response, token)
        return view(db, user, body.role, csrf)


@router.get("/auth/me", response_model=AuthView)
def me(request: Request, principal: Authenticated) -> AuthView:
    """REQ-AUTH-01/04: return the stable per-session synchroniser token."""
    with database(request.app.state.settings) as db:
        csrf = csrf_token(request.cookies["pfc_session"])
        return view(db, principal.user, principal.session.role_at_login, csrf)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, principal: Authenticated) -> None:
    """REQ-AUTH-04/05: revoke server-side session and audit logout."""
    with database(request.app.state.settings) as db:
        now = stamp(request.app.state.clock())
        sessions.revoke_session(db, principal.session.session_id, now)
        audit.append_audit(db, ts=now, user_id=principal.user.user_id, action="LOGOUT")
    response.delete_cookie("pfc_session", path="/", httponly=True, samesite="strict")


@router.post("/auth/password", response_model=AuthView)
def password(
    body: ChangePassword, request: Request, response: Response, principal: Authenticated
) -> AuthView:
    """REQ-AUTH-02/03/04/05: verify, change password and rotate the session."""
    settings = request.app.state.settings
    now = request.app.state.clock()
    with database(settings) as db:
        user = users.get_user_by_id(db, principal.user.user_id)
        assert user is not None
        if not verify(user.password_hash, body.current_password, settings):
            audit.append_audit(db, ts=stamp(now), user_id=user.user_id, action="LOGIN_FAIL")
            db.commit()
            raise Problem(401, "INVALID_CREDENTIALS", "invalid credentials")
        assigned = db.execute("SELECT name FROM roles WHERE role_id=?", (user.role_id,)).fetchone()[
            0
        ]
        validate_password(body.new_password, user.email, assigned)
        users.update_password(
            db,
            user.user_id,
            password_hash=hasher(settings).hash(body.new_password),
            must_change_password=False,
            now=stamp(now),
        )
        db.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (stamp(now), user.user_id),
        )
        token, csrf = issue(db, settings, user.user_id, principal.session.role_at_login, now)
        audit.append_audit(db, ts=stamp(now), user_id=user.user_id, action="PASSWORD_CHANGE")
        cookie(response, token)
        updated = users.get_user_by_id(db, user.user_id)
        assert updated is not None
        return view(db, updated, principal.session.role_at_login, csrf)
