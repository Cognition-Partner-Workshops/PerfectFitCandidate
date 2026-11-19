"""Discover dependent rows without moving or deleting audit/history records."""

import re
import sqlite3
from typing import Any

from pfc.services.lifecycle import record_transition


def vector_tables(db: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if row[0] in {"vec_chunks", "pfc_vector_records"}
        or re.fullmatch(r"vec_chunks_d[1-9][0-9]*", row[0])
    ]


def delete_chunks(db: sqlite3.Connection, ids: list[int]) -> None:
    for chunk_id in ids:
        for name in vector_tables(db):
            db.execute(f'DELETE FROM "{name}" WHERE rowid=?', (chunk_id,))
        db.execute("DELETE FROM embedding_chunks WHERE chunk_id=?", (chunk_id,))


def purge_entities(
    db: sqlite3.Connection,
    items: list[dict[str, Any]],
    *,
    user_id: int,
    batch_id: int,
    now: str,
) -> None:
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND sql NOT LIKE 'CREATE VIRTUAL%'"
        )
    ]
    protected = {"audit_log", "lifecycle_events", "archive_batches", "archive_batch_items"}
    edges: dict[str, list[tuple[str, str, str]]] = {}
    for table in tables:
        if table in protected:
            continue
        for fk in db.execute(f'PRAGMA foreign_key_list("{table}")'):
            edges.setdefault(fk[2], []).append((table, fk[3], fk[4]))
    visited: set[tuple[str, int]] = set()
    ordered: list[tuple[str, int]] = []

    def discover(table: str, rowid: int) -> None:
        key = (table, rowid)
        if key in visited or table in protected:
            return
        visited.add(key)
        row = db.execute(f'SELECT * FROM "{table}" WHERE rowid=?', (rowid,)).fetchone()
        if row is None:
            return
        for child, child_column, parent_column in edges.get(table, []):
            for result in db.execute(
                f'SELECT rowid FROM "{child}" WHERE "{child_column}"=?', (row[parent_column],)
            ).fetchall():
                discover(child, result[0])
        ordered.append(key)

    purged_resources = {item["entity_id"] for item in items if item["entity_type"] == "resource"}
    candidates: set[int] = set()
    for item in items:
        entity, entity_id = item["entity_type"], item["entity_id"]
        record_transition(
            db,
            entity,
            entity_id,
            "archived",
            "purged",
            "purge",
            now=now,
            user_id=user_id,
            archive_batch_id=batch_id,
        )
        db.execute(
            "UPDATE lifecycle_events SET comment=NULL WHERE entity_type=? AND entity_id=?",
            (entity, entity_id),
        )
        if entity == "resource":
            candidate = db.execute(
                "SELECT candidate_id FROM resources WHERE resource_id=?", (entity_id,)
            ).fetchone()[0]
            if candidate is not None:
                candidates.add(candidate)
        discover(entity + "s", entity_id)
        db.execute(
            "DELETE FROM occ_approvals WHERE entity_type=? AND entity_id=?", (entity, entity_id)
        )
        db.execute("DELETE FROM summaries WHERE owner_kind=? AND owner_id=?", (entity, entity_id))
    for candidate in candidates:
        linked = {
            row[0]
            for row in db.execute(
                "SELECT resource_id FROM resources WHERE candidate_id=?", (candidate,)
            )
        }
        if linked <= purged_resources:
            discover("candidates", candidate)
            db.execute(
                "DELETE FROM summaries WHERE owner_kind='candidate' AND owner_id=?", (candidate,)
            )
    owners = {
        "demands": None,
        "demand_versions": "jd",
        "jd_requirement_items": "jd_item",
        "resources": "ct_skills",
        "cv_versions": "cv",
    }
    for table, rowid in ordered:
        owner = owners.get(table)
        if owner:
            delete_chunks(
                db,
                [
                    r[0]
                    for r in db.execute(
                        "SELECT chunk_id FROM embedding_chunks WHERE owner_type=? AND owner_id=?",
                        (owner, rowid),
                    )
                ],
            )
    db.execute("PRAGMA defer_foreign_keys=ON")
    for table, rowid in ordered:
        db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,))
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise sqlite3.IntegrityError("Purge left dependent rows")
