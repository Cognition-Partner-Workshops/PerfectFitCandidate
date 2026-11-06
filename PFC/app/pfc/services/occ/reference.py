import json
import sqlite3
from typing import Any

from pfc.security.problems import Problem
from pfc.services.occ.common import audit_write, get_row, now


def list_rows(db: sqlite3.Connection, user_id: int, table: str) -> list[dict[str, Any]]:
    """REQ-ELIG-02/03/07, REQ-GRD-01: editable reference lists."""
    order = "ordinal" if table == "grades" else "rowid"
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def save_bench(
    db: sqlite3.Connection, user_id: int, data: dict[str, Any], code: str | None = None
) -> dict[str, Any]:
    before = get_row(db, "bench_statuses", "code", code) if code is not None else None
    if code is not None and data["code"] != code:
        raise Problem(422, "VALIDATION_ERROR", "Status code cannot change")
    if before is None:
        db.execute(
            "INSERT INTO bench_statuses(code,label,is_excluded,priority,flag_in_gaps,"
            "available_from_bench_start) VALUES (:code,:label,:is_excluded,:priority,"
            ":flag_in_gaps,:available_from_bench_start)",
            data,
        )
    else:
        db.execute(
            "UPDATE bench_statuses SET label=:label,is_excluded=:is_excluded,priority=:priority,"
            "flag_in_gaps=:flag_in_gaps,available_from_bench_start=:available_from_bench_start "
            "WHERE code=:code",
            data,
        )
        for row in db.execute(
            "SELECT * FROM status_map WHERE canonical_code=? ORDER BY source_value", (code,)
        ).fetchall():
            mapping = {key: row[key] for key in ("canonical_code", "priority", "is_excluded")}
            for field in ("priority", "is_excluded"):
                if data[field] is not None and mapping[field] == before[field]:
                    mapping[field] = data[field]
            if any(mapping[field] != row[field] for field in ("priority", "is_excluded")):
                save_mapping(db, user_id, row["source_value"], mapping)
    audit_write(db, user_id, "OCC_EDIT", "bench_statuses", data["code"], before, data)
    return get_row(db, "bench_statuses", "code", data["code"])


def delete_bench(db: sqlite3.Connection, user_id: int, code: str) -> None:
    before = get_row(db, "bench_statuses", "code", code)
    if db.execute("SELECT 1 FROM status_map WHERE canonical_code=?", (code,)).fetchone():
        raise Problem(409, "CONFLICT", "Status is referenced by status_map")
    db.execute("DELETE FROM bench_statuses WHERE code=?", (code,))
    audit_write(db, user_id, "OCC_EDIT", "bench_statuses", code, before, None)


def save_mapping(
    db: sqlite3.Connection, user_id: int, source: str, data: dict[str, Any]
) -> dict[str, Any]:
    if (
        data["canonical_code"] is not None
        and not db.execute(
            "SELECT 1 FROM bench_statuses WHERE code=?", (data["canonical_code"],)
        ).fetchone()
    ):
        raise Problem(422, "VALIDATION_ERROR", "Unknown canonical status")
    row = db.execute("SELECT * FROM status_map WHERE source_value=?", (source,)).fetchone()
    before = dict(row) if row else None
    db.execute(
        "INSERT INTO status_map(source_value,canonical_code,priority,is_excluded,"
        "mapped_by,updated_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(source_value) DO UPDATE SET "
        "canonical_code=excluded.canonical_code,priority=excluded.priority,"
        "is_excluded=excluded.is_excluded,mapped_by=excluded.mapped_by,updated_at=excluded.updated_at",
        (source, data["canonical_code"], data["priority"], data["is_excluded"], user_id, now()),
    )
    audit_write(db, user_id, "OCC_EDIT", "status_map", source, before, data)
    return get_row(db, "status_map", "source_value", source)


def delete_mapping(db: sqlite3.Connection, user_id: int, source: str) -> None:
    before = get_row(db, "status_map", "source_value", source)
    db.execute("DELETE FROM status_map WHERE source_value=?", (source,))
    audit_write(db, user_id, "OCC_EDIT", "status_map", source, before, None)


def replace_grades(
    db: sqlite3.Connection, user_id: int, grades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    codes = [g["code"] for g in grades]
    if len(set(codes)) != len(codes):
        raise Problem(422, "VALIDATION_ERROR", "Grade codes must be unique")
    before = list_rows(db, user_id, "grades")
    # Free unique ordinals before assigning the replacement order.
    offset = max((row["ordinal"] for row in before), default=0) + len(before) + len(grades) + 1
    db.execute("UPDATE grades SET ordinal=ordinal+?", (offset,))
    for ordinal, grade in enumerate(grades, 1):
        db.execute(
            "INSERT INTO grades(code,ordinal,active) VALUES (?,?,?) ON CONFLICT(code) "
            "DO UPDATE SET ordinal=excluded.ordinal,active=excluded.active",
            (grade["code"], ordinal, grade["active"]),
        )
    for old in before:
        if old["code"] not in codes:
            db.execute("DELETE FROM grades WHERE code=?", (old["code"],))
    audit_write(
        db,
        user_id,
        "OCC_EDIT",
        "grades",
        "all",
        {"grades": before},
        {"grades": list_rows(db, user_id, "grades")},
    )
    return list_rows(db, user_id, "grades")


def get_locations(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT value_json FROM settings WHERE scope='system' AND user_id IS NULL "
        "AND key='location.aliases'"
    ).fetchone()
    return json.loads(row[0]) if row else {"aliases": {}, "travel_clusters": {}}


def save_locations(db: sqlite3.Connection, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    before = get_locations(db, user_id)
    db.execute(
        "INSERT INTO settings(scope,key,value_json,admin_only,updated_by,updated_at) "
        "VALUES ('system','location.aliases',?,1,?,?) ON CONFLICT DO UPDATE SET "
        "value_json=excluded.value_json,admin_only=1,updated_by=excluded.updated_by,"
        "updated_at=excluded.updated_at",
        (json.dumps(data), user_id, now()),
    )
    audit_write(db, user_id, "SETTING_CHANGE", "settings", "location.aliases", before, data)
    return data
