import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    session_id: int
    token_hash: str
    csrf_hash: str
    user_id: int
    role_at_login: str
    created_at: str
    last_seen_at: str
    expires_at: str
    revoked_at: str | None


def get_session_by_token_hash(db: sqlite3.Connection, token_hash: str) -> Session | None:
    row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
    return Session(**dict(row)) if row else None


def create_session(
    db: sqlite3.Connection,
    *,
    token_hash: str,
    csrf_hash: str,
    user_id: int,
    role_at_login: str,
    now: str,
    expires_at: str,
) -> Session:
    db.execute(
        "INSERT INTO sessions(token_hash, csrf_hash, user_id, role_at_login, created_at, "
        "last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_hash, csrf_hash, user_id, role_at_login, now, now, expires_at),
    )
    session = get_session_by_token_hash(db, token_hash)
    assert session is not None
    return session


def touch_session(db: sqlite3.Connection, session_id: int, now: str, expires_at: str) -> None:
    db.execute(
        "UPDATE sessions SET last_seen_at=?, expires_at=? "
        "WHERE session_id=? AND revoked_at IS NULL",
        (now, expires_at, session_id),
    )


def revoke_session(db: sqlite3.Connection, session_id: int, now: str) -> None:
    db.execute(
        "UPDATE sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
        (now, session_id),
    )


def purge_expired_sessions(db: sqlite3.Connection, now: str) -> int:
    return db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,)).rowcount
