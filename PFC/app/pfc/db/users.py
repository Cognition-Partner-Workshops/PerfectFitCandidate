import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    user_id: int
    email: str
    display_name: str
    role_id: int
    persona: str | None
    password_hash: str
    must_change_password: int
    failed_attempts: int
    locked_until: str | None
    is_active: int
    created_at: str
    updated_at: str
    last_login_at: str | None


def get_user_by_id(db: sqlite3.Connection, user_id: int) -> User | None:
    row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return User(**dict(row)) if row else None


def get_user_by_email(db: sqlite3.Connection, email: str) -> User | None:
    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return User(**dict(row)) if row else None


def create_user(
    db: sqlite3.Connection,
    *,
    email: str,
    display_name: str,
    role_id: int,
    password_hash: str,
    now: str,
    must_change_password: bool = True,
    persona: str | None = None,
) -> User:
    cursor = db.execute(
        "INSERT INTO users(email, display_name, role_id, password_hash, created_at, "
        "updated_at, must_change_password, persona) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (email, display_name, role_id, password_hash, now, now, must_change_password, persona),
    )
    user = get_user_by_id(db, int(cursor.lastrowid or 0))
    assert user is not None
    return user


def update_login_state(
    db: sqlite3.Connection,
    user_id: int,
    *,
    failed_attempts: int,
    locked_until: str | None,
    now: str,
) -> None:
    db.execute(
        "UPDATE users SET failed_attempts=?, locked_until=?, updated_at=? WHERE user_id=?",
        (failed_attempts, locked_until, now, user_id),
    )


def update_password(
    db: sqlite3.Connection,
    user_id: int,
    *,
    password_hash: str,
    must_change_password: bool,
    now: str,
) -> None:
    db.execute(
        "UPDATE users SET password_hash=?, must_change_password=?, updated_at=? WHERE user_id=?",
        (password_hash, must_change_password, now, user_id),
    )


def count_admins(db: sqlite3.Connection) -> int:
    return int(
        db.execute(
            "SELECT count(*) FROM users JOIN roles USING(role_id) "
            "WHERE roles.name='Admin' AND users.is_active=1"
        ).fetchone()[0]
    )
