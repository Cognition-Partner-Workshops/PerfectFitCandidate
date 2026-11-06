import secrets
import sqlite3
import string
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pfc.config import Settings
from pfc.db import users
from pfc.security.passwords import hasher, validate_password
from pfc.security.problems import Problem
from pfc.security.sessions import parse
from pfc.services.occ.common import audit_write, get_row, now
from pfc.services.pii import mask_email


def list_users(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """REQ-ROLE-03: list identities without credential material."""
    result = []
    for row in db.execute(
        "SELECT u.*, r.name AS role FROM users u JOIN roles r USING(role_id) ORDER BY user_id"
    ):
        item = dict(row)
        item.pop("password_hash")
        item["email"] = mask_email(item["email"])
        item["status"] = (
            "Disabled"
            if not item["is_active"]
            else (
                "Locked"
                if item["locked_until"] and parse(item["locked_until"]) > datetime.now(UTC)
                else "Active"
            )
        )
        result.append(item)
    return result


def one_time_password(email: str, role: str) -> str:
    while True:
        password = "".join(
            secrets.choice(string.ascii_letters + string.digits + "!#$%&*?") for _ in range(32)
        )
        try:
            validate_password(password, email, role)
            return password
        except Problem:
            pass


def create_user(
    db: sqlite3.Connection, user_id: int, data: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """REQ-AUTH-02/04, REQ-ROLE-03: issue a one-time password, returned once."""
    if users.get_user_by_email(db, data["email"]):
        raise Problem(409, "CONFLICT", "Email already exists")
    role_id = db.execute("SELECT role_id FROM roles WHERE name=?", (data["role"],)).fetchone()[0]
    password = one_time_password(data["email"], data["role"])
    created = users.create_user(
        db,
        email=data["email"],
        display_name=data["display_name"],
        role_id=role_id,
        password_hash=hasher(settings).hash(password),
        now=now(),
        persona=data.get("persona"),
    )
    audit_write(db, user_id, "USER_CREATE", "users", created.user_id, None, asdict(created))
    return {
        "user": next(u for u in list_users(db, user_id) if u["user_id"] == created.user_id),
        "one_time_password": password,
    }


def update_user(
    db: sqlite3.Connection, user_id: int, target: int, data: dict[str, Any]
) -> dict[str, Any]:
    before = get_row(db, "users", "user_id", target)
    admin_id = db.execute("SELECT role_id FROM roles WHERE name='Admin'").fetchone()[0]
    removes_admin = data.get("role", "Admin") != "Admin" or data.get("is_active") is False
    if removes_admin and before["role_id"] == admin_id:
        if before["is_active"] and users.count_admins(db) <= 1:
            raise Problem(409, "LAST_ADMIN", "Cannot remove the last active Admin")
        if target == user_id:
            raise Problem(409, "SELF_ADMIN", "Cannot disable or demote yourself")
    changes = dict(data)
    if "role" in changes:
        changes["role_id"] = db.execute(
            "SELECT role_id FROM roles WHERE name=?", (changes.pop("role"),)
        ).fetchone()[0]
    changes["updated_at"] = now()
    db.execute(
        "UPDATE users SET " + ", ".join(f"{key}=?" for key in changes) + " WHERE user_id=?",
        (*changes.values(), target),
    )
    if changes.get("role_id", before["role_id"]) != before["role_id"] or (
        before["is_active"] and changes.get("is_active") is False
    ):
        db.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now(), target),
        )
    audit_write(
        db, user_id, "USER_UPDATE", "users", target, before, get_row(db, "users", "user_id", target)
    )
    return next(u for u in list_users(db, user_id) if u["user_id"] == target)


def unlock(db: sqlite3.Connection, user_id: int, target: int) -> dict[str, Any]:
    before = get_row(db, "users", "user_id", target)
    users.update_login_state(db, target, failed_attempts=0, locked_until=None, now=now())
    audit_write(
        db, user_id, "USER_UNLOCK", "users", target, before, get_row(db, "users", "user_id", target)
    )
    return next(u for u in list_users(db, user_id) if u["user_id"] == target)


def reset_password(
    db: sqlite3.Connection, user_id: int, target: int, settings: Settings
) -> dict[str, Any]:
    before = get_row(db, "users", "user_id", target)
    role = db.execute("SELECT name FROM roles WHERE role_id=?", (before["role_id"],)).fetchone()[0]
    password = one_time_password(before["email"], role)
    users.update_password(
        db,
        target,
        password_hash=hasher(settings).hash(password),
        must_change_password=True,
        now=now(),
    )
    users.update_login_state(db, target, failed_attempts=0, locked_until=None, now=now())
    db.execute(
        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now(), target)
    )
    audit_write(
        db,
        user_id,
        "USER_RESET_PASSWORD",
        "users",
        target,
        before,
        get_row(db, "users", "user_id", target),
    )
    return {"user_id": target, "one_time_password": password}
