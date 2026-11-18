"""Atomic lifecycle changes and append-only history."""

import sqlite3
from typing import Any

from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.services.matching.common import atomic
from pfc.services.summaries import redact_contacts

METADATA = frozenset({"end_outcome", "end_source", "ended_at", "archived_at", "archive_batch_id"})
REASONS = {
    "filled": {"position_filled", "other"},
    "cancelled": {"request_cancelled", "other"},
    "closed": {"no_longer_required", "other"},
    "off_bench": {"assigned", "other"},
    "released": {"left_organization", "other"},
    "reopen": {"correction", "new_requirement", "other"},
}


def entity_table(entity_type: str) -> str:
    if entity_type not in {"demand", "resource"}:
        raise Problem(422, "INVALID_ENTITY", "Expected demand or resource")
    return entity_type + "s"


def record_transition(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    from_state: str | None,
    to_state: str,
    source: str,
    *,
    now: str,
    outcome: str | None = None,
    reason_code: str | None = None,
    comment: str | None = None,
    ingestion_run_id: int | None = None,
    archive_batch_id: int | None = None,
    user_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    table = entity_table(entity_type)
    with atomic(db, immediate=True):
        row = db.execute(
            f"SELECT lifecycle FROM {table} WHERE {entity_type}_id=?", (entity_id,)
        ).fetchone()
        if row is None:
            raise Problem(404, "ENTITY_NOT_FOUND", "Demand or resource not found")
        if from_state is not None and row[0] != from_state:
            raise Problem(409, "LIFECYCLE_CONFLICT", "Lifecycle changed; reload the record")
        fields = dict(metadata or {})
        if fields.keys() - METADATA:
            raise ValueError("Unsupported lifecycle metadata")
        fields.update(lifecycle=to_state, updated_at=now)
        db.execute(
            f"UPDATE {table} SET "
            + ",".join(f"{key}=?" for key in fields)
            + f" WHERE {entity_type}_id=?",
            (*fields.values(), entity_id),
        )
        db.execute(
            "INSERT INTO lifecycle_events(entity_type,entity_id,from_state,to_state,outcome,"
            "reason_code,comment,source,ingestion_run_id,archive_batch_id,user_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                from_state,
                to_state,
                outcome,
                reason_code,
                redact_contacts(comment) if comment else None,
                source,
                ingestion_run_id,
                archive_batch_id,
                user_id,
                now,
            ),
        )


def manual_change(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    *,
    outcome: str,
    reason_code: str,
    comment: str | None,
    expected_updated_at: str,
    expected_event_id: int,
    user_id: int,
    now: str,
) -> dict[str, Any]:
    table = entity_table(entity_type)
    if (
        reason_code not in REASONS.get(outcome, set())
        or (reason_code == "other" and not (comment or "").strip())
        or len(comment or "") > 500
    ):
        raise Problem(422, "INVALID_REASON", "Choose a valid reason; other requires a comment")
    with atomic(db, immediate=True):
        row = db.execute(f"SELECT * FROM {table} WHERE {entity_type}_id=?", (entity_id,)).fetchone()
        if row is None:
            raise Problem(404, "ENTITY_NOT_FOUND", "Demand or resource not found")
        active, ended = ("open", "closed") if entity_type == "demand" else ("active", "off_bench")
        latest_event_id = db.execute(
            "SELECT coalesce(max(event_id),0) FROM lifecycle_events "
            "WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        ).fetchone()[0]
        if row["updated_at"] != expected_updated_at or latest_event_id != expected_event_id:
            raise Problem(409, "LIFECYCLE_CONFLICT", "Record changed; reload the record")
        if outcome == "reopen":
            allowed = row["lifecycle"] == ended and row["end_source"] == "manual"
            target = active
            metadata = dict.fromkeys(METADATA)
        else:
            outcomes = (
                {"filled", "cancelled", "closed"}
                if entity_type == "demand"
                else {"off_bench", "released"}
            )
            allowed = outcome in outcomes and (
                row["lifecycle"] == active
                or (row["lifecycle"] == ended and row["end_source"] in {None, "upload"})
            )
            target = ended
            metadata = {"end_source": "manual", "end_outcome": outcome, "ended_at": now}
        if not allowed:
            raise Problem(409, "LIFECYCLE_CONFLICT", "Record cannot make this transition")
        record_transition(
            db,
            entity_type,
            entity_id,
            row["lifecycle"],
            target,
            "manual",
            now=now,
            outcome=None if outcome == "reopen" else outcome,
            reason_code=reason_code,
            comment=comment,
            user_id=user_id,
            metadata=metadata,
        )
        append_audit(
            db,
            ts=now,
            user_id=user_id,
            action="LIFECYCLE_CHANGE",
            entity_type=entity_type,
            entity_id=str(entity_id),
            details={
                "from": row["lifecycle"],
                "to": target,
                "outcome": outcome,
                "reason_code": reason_code,
            },
        )
        return dict(
            db.execute(
                f"SELECT lifecycle,updated_at,end_source,end_outcome FROM {table} "
                f"WHERE {entity_type}_id=?",
                (entity_id,),
            ).fetchone()
        )


def events(
    db: sqlite3.Connection,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    cursor: int = 0,
) -> list[dict[str, Any]]:
    if entity_type is not None:
        entity_table(entity_type)
    clauses = ["event_id>?"]
    params: list[Any] = [cursor]
    for column, operator, value in (
        ("entity_type", "=", entity_type),
        ("entity_id", "=", entity_id),
        ("created_at", ">=", since),
        ("created_at", "<=", until),
    ):
        if value is not None:
            clauses.append(f"{column}{operator}?")
            params.append(value)
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM lifecycle_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_id LIMIT ?",
            (*params, limit),
        )
    ]
