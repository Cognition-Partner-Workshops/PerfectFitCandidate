import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from pfc.config import Settings
from pfc.db import sessions


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def issue(
    db: sqlite3.Connection,
    settings: Settings,
    user_id: int,
    role: str,
    now: datetime,
) -> tuple[str, str]:
    """REQ-AUTH-04: store only hashes of random session and synchroniser tokens."""
    token = secrets.token_urlsafe(32)
    csrf = csrf_token(token)
    expires = min(
        now + timedelta(minutes=settings.idle_timeout_min),
        now + timedelta(hours=settings.absolute_timeout_h),
    )
    sessions.create_session(
        db,
        token_hash=digest(token),
        csrf_hash=digest(csrf),
        user_id=user_id,
        role_at_login=role,
        now=stamp(now),
        expires_at=stamp(expires),
    )
    return token, csrf


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"pfc-session-csrf-v1", hashlib.sha256).hexdigest()
