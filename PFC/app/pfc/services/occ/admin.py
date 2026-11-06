import csv
import io
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from pfc.db.audit import verify_chain
from pfc.security.problems import Problem
from pfc.services.occ.common import audit_write, get_row, now


def recipients(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """REQ-EML-05: only explicitly entered resource-team addresses."""
    return [
        dict(r) for r in db.execute("SELECT * FROM resource_team_recipients ORDER BY recipient_id")
    ]


def save_recipient(
    db: sqlite3.Connection, user_id: int, data: dict[str, Any], target: int | None = None
) -> dict[str, Any]:
    before = get_row(db, "resource_team_recipients", "recipient_id", target) if target else None
    if target is None:
        cursor = db.execute(
            "INSERT INTO resource_team_recipients(email,display_name,added_by,added_at) "
            "VALUES (?,?,?,?)",
            (data["email"], data.get("display_name"), user_id, now()),
        )
        target = int(cursor.lastrowid or 0)
    elif data:
        db.execute(
            "UPDATE resource_team_recipients SET "
            + ",".join(f"{key}=?" for key in data)
            + " WHERE recipient_id=?",
            (*data.values(), target),
        )
    after = get_row(db, "resource_team_recipients", "recipient_id", target)
    audit_write(db, user_id, "OCC_EDIT", "resource_team_recipients", target, before, after)
    return after


def delete_recipient(db: sqlite3.Connection, user_id: int, target: int) -> None:
    before = get_row(db, "resource_team_recipients", "recipient_id", target)
    db.execute("DELETE FROM resource_team_recipients WHERE recipient_id=?", (target,))
    audit_write(db, user_id, "OCC_EDIT", "resource_team_recipients", target, before, None)


def normalize_domain(domain: str) -> str:
    domain = domain.strip().lstrip("@").strip().lower()
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(domain) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", domain):
        raise Problem(422, "VALIDATION_ERROR", "Invalid domain")
    return domain


def domains(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """REQ-CLS-01: explicit normalized internal-domain allowlist."""
    return [dict(r) for r in db.execute("SELECT * FROM internal_email_domains ORDER BY domain")]


def add_domain(db: sqlite3.Connection, user_id: int, domain: str) -> dict[str, Any]:
    domain = normalize_domain(domain)
    db.execute(
        "INSERT INTO internal_email_domains(domain,added_by,added_at) VALUES (?,?,?)",
        (domain, user_id, now()),
    )
    after = get_row(db, "internal_email_domains", "domain", domain)
    audit_write(db, user_id, "OCC_EDIT", "internal_email_domains", domain, None, after)
    return after


def delete_domain(db: sqlite3.Connection, user_id: int, domain: str) -> None:
    domain = normalize_domain(domain)
    before = get_row(db, "internal_email_domains", "domain", domain)
    db.execute("DELETE FROM internal_email_domains WHERE domain=?", (domain,))
    audit_write(db, user_id, "OCC_EDIT", "internal_email_domains", domain, before, None)


def settings_get(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """REQ-ROLE-04: system settings, excluding dedicated reference endpoints."""
    return {
        row["key"]: json.loads(row["value_json"])
        for row in db.execute(
            "SELECT key,value_json FROM settings WHERE scope='system' AND user_id IS NULL "
            "AND key!='location.aliases' ORDER BY key"
        )
    }


def settings_put(db: sqlite3.Connection, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
    for key in values:
        if key == "location.aliases" or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", key):
            raise Problem(422, "VALIDATION_ERROR", "Invalid or reserved settings key")
    before = settings_get(db, user_id)
    try:
        encoded = {key: json.dumps(value, allow_nan=False) for key, value in values.items()}
    except (ValueError, TypeError) as exc:
        raise Problem(422, "VALIDATION_ERROR", "Settings values must be JSON") from exc
    for key, value in values.items():
        if key in before and before[key] == value:
            continue
        db.execute(
            "INSERT INTO settings(scope,key,value_json,admin_only,updated_by,updated_at)"
            " VALUES ('system',?,?,1,?,?) ON CONFLICT DO UPDATE SET value_json=excluded.value_json,"
            "admin_only=1,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (key, encoded[key], user_id, now()),
        )
        secret = any(marker in key for marker in ("password", "secret", "token", "credential"))
        old = {"value": "[REDACTED]" if secret else before[key]} if key in before else None
        new = {"value": "[REDACTED]" if secret else value}
        audit_write(db, user_id, "SETTING_CHANGE", "settings", key, old, new)
    return settings_get(db, user_id)


def audit_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds")


def audit_query(
    db: sqlite3.Connection, user_id: int, filters: dict[str, Any], limit: int, offset: int
) -> dict[str, Any]:
    """REQ-AUTH-05: filtered audit history with complete-chain verification."""
    clauses, params = [], []
    for key in ("user_id", "entity_type", "entity_id"):
        if filters.get(key) is not None:
            clauses.append(f"{key}=?")
            params.append(filters[key])
    if filters.get("action"):
        clauses.append("action IN (" + ",".join("?" for _ in filters["action"]) + ")")
        params.extend(filters["action"])
    for key, operator in (("from", ">="), ("to", "<=")):
        if filters.get(key):
            db.create_function("audit_timestamp", 1, audit_timestamp, deterministic=True)
            clauses.append(f"audit_timestamp(ts){operator}?")
            params.append(audit_timestamp(filters[key]))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = db.execute("SELECT count(*) FROM audit_log" + where, params).fetchone()[0]
    rows = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM audit_log" + where + " ORDER BY audit_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
    ]
    return {"items": rows, "total": total, "chain_valid": verify_chain(db)}


def export_audit(
    db: sqlite3.Connection, user_id: int, filters: dict[str, Any], limit: int, offset: int
) -> str:
    data = audit_query(db, user_id, filters, limit, offset)
    columns = [
        "audit_id",
        "ts",
        "user_id",
        "action",
        "entity_type",
        "entity_id",
        "details_json",
        "row_hash",
    ]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    for row in data["items"]:
        cells = []
        for key in columns:
            value = str(row[key]) if row[key] is not None else ""
            cells.append("'" + value if value.startswith(("=", "+", "-", "@", "\t")) else value)
        writer.writerow(cells)
    audit_write(
        db, user_id, "AUDIT_EXPORT", "audit_log", "export", None, {"count": len(data["items"])}
    )
    return stream.getvalue()
