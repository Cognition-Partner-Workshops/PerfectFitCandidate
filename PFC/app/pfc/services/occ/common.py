import sqlite3
from datetime import UTC, datetime
from typing import Any

from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.security.secrets_store import sensitive_setting
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

    def safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: "[REDACTED]" if sensitive_setting(k, v) else safe(v) for k, v in value.items()
            }
        if isinstance(value, list):
            return [safe(v) for v in value]
        return value

    pii = entity_type in {"users", "resource_team_recipients"}
    append_audit(
        db,
        ts=now(),
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details={
            "before": (sorted(before) if pii else safe(before)) if before is not None else None,
            "after": (sorted(after) if pii else safe(after)) if after is not None else None,
        },
    )


def get_row(db: sqlite3.Connection, table: str, key: str, value: str | int) -> dict[str, Any]:
    row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    if row is None:
        raise Problem(404, "NOT_FOUND", "Record not found")
    return dict(row)
