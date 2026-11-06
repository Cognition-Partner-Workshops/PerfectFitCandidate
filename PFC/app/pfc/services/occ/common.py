import sqlite3
from datetime import UTC, datetime
from typing import Any

from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.security.secrets_store import redact_sensitive
from pfc.security.sessions import stamp


def now() -> str:
    return stamp(datetime.now(UTC))


def audit_write(
    db: sqlite3.Connection,
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: str | int,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    """REQ-AUTH-05: retain values except for PII entities and secret fields."""

    pii = entity_type in {"users", "resource_team_recipients"}
    store = getattr(db, "secret_store", None)
    append_audit(
        db,
        ts=now(),
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details={
            "before": (sorted(before) if pii else redact_sensitive(before, store=store))
            if before is not None
            else None,
            "after": (sorted(after) if pii else redact_sensitive(after, store=store))
            if after is not None
            else None,
        },
    )


def get_row(db: sqlite3.Connection, table: str, key: str, value: str | int) -> dict[str, Any]:
    row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    if row is None:
        raise Problem(404, "NOT_FOUND", "Record not found")
    return dict(row)
