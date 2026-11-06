import hashlib
import json
import sqlite3
from typing import Any

GENESIS = "0" * 64


def _hash(prev_hash: str, row: dict[str, Any]) -> str:
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def append_audit(
    db: sqlite3.Connection,
    *,
    ts: str,
    action: str,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Append within the caller's transaction, serializing concurrent writers."""
    db.execute("INSERT INTO audit_log(audit_id) SELECT NULL WHERE 0")
    last = db.execute(
        "SELECT audit_id, row_hash FROM audit_log ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    audit_id = last[0] + 1 if last else 1
    previous = last[1] if last else GENESIS
    row = {
        "audit_id": audit_id,
        "ts": ts,
        "user_id": user_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details_json": json.dumps(
            details, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if details is not None
        else None,
    }
    db.execute(
        "INSERT INTO audit_log(audit_id, ts, user_id, action, entity_type, entity_id, "
        "details_json, prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*row.values(), previous, _hash(previous, row)),
    )
    return audit_id


def verify_chain(db: sqlite3.Connection) -> bool:
    previous = GENESIS
    for record in db.execute("SELECT * FROM audit_log ORDER BY audit_id"):
        row = dict(record)
        row_hash = row.pop("row_hash")
        prev_hash = row.pop("prev_hash")
        if prev_hash != previous or row_hash != _hash(previous, row):
            return False
        previous = row_hash
    return True
