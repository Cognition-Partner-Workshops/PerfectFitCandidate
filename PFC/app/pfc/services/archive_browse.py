"""Masked archived entity search and spreadsheet-safe exports."""

import csv
import io
import sqlite3
from typing import Any

from pfc.db.audit import append_audit
from pfc.services.matching.export import safe_cell
from pfc.services.pii import mask_email, mask_emails_in

FIELDS = {
    "demands": (
        "demand_id",
        "code_requisition",
        "account_name",
        "demand_request_name",
        "region",
        "code_local_grade",
        "bu",
        "lifecycle",
        "end_outcome",
        "ended_at",
        "archived_at",
    ),
    "resources": (
        "resource_id",
        "candidate_id",
        "short_name",
        "email_address",
        "grade",
        "location",
        "bench_status",
        "group_name",
        "lifecycle",
        "end_outcome",
        "ended_at",
        "archived_at",
    ),
}


def search(
    db: sqlite3.Connection,
    collection: str,
    *,
    q: str | None = None,
    grade: str | None = None,
    account: str | None = None,
    location: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    clauses, params = ["lifecycle='archived'"], []
    fields = FIELDS[collection]
    if q:
        columns = (
            ("code_requisition", "account_name", "demand_request_name")
            if collection == "demands"
            else ("short_name",)
        )
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in columns) + ")")
        params.extend([f"%{q}%"] * len(columns))
    for column, value in (
        ("code_local_grade" if collection == "demands" else "grade", grade),
        ("account_name" if collection == "demands" else "group_name", account),
        ("region" if collection == "demands" else "location", location),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    where = " AND ".join(clauses)
    total = db.execute(f"SELECT count(*) FROM {collection} WHERE {where}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT {','.join(fields)} FROM {collection} WHERE {where} "
        f"ORDER BY {fields[0]} LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    )
    items = []
    for row in rows:
        item = {
            key: mask_emails_in(value) if isinstance(value, str) else value
            for key, value in dict(row).items()
        }
        if "email_address" in item:
            item["email_address"] = mask_email(row["email_address"])
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def export_csv(
    db: sqlite3.Connection, collection: str, user_id: int, now: str, **filters: Any
) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(FIELDS[collection])
    page = 1
    count = 0
    while True:
        result = search(db, collection, page=page, page_size=500, **filters)
        for item in result["items"]:
            writer.writerow(
                [
                    safe_cell(str(item[field])) if item[field] is not None else ""
                    for field in FIELDS[collection]
                ]
            )
        count += len(result["items"])
        if count >= result["total"]:
            break
        page += 1
    append_audit(
        db,
        ts=now,
        user_id=user_id,
        action="ARCHIVE_EXPORT",
        entity_type=collection,
        details={"count": count, "masked": True},
    )
    return "\ufeff" + stream.getvalue()
