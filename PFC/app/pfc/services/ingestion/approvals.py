"""REQ-ING-03 / REQ-ROLE-03: atomic upload snapshots and OCC decisions."""

import json
import re
import sqlite3
from datetime import date
from typing import Any

from pydantic.alias_generators import to_camel

from pfc.api.schemas.occ_approvals import ApprovalDecision, ApprovalEntry, ApprovalField
from pfc.db.audit import append_audit
from pfc.domain.normalise import ParsedDate
from pfc.domain.scoring.engine import build_ct_basis, eligibility
from pfc.domain.scoring.models import Resource
from pfc.security.problems import Problem

from .browse import _mask
from .repository import domain_config

EDITABLE = {
    "demand": frozenset(
        {
            "position_start_date",
            "account_name",
            "code_local_grade",
            "demand_request_name",
            "region",
            "bu",
            "owner",
        }
    ),
    "resource": frozenset(
        {
            "grade",
            "location",
            "bench_status",
            "skills_raw",
            "available_capacity",
            "bench_start_date",
            "bench_end_date",
            "tracker_text",
            "tracker_url",
            "group_name",
            "group_owner",
        }
    ),
}
DISPLAY = {
    "demand": EDITABLE["demand"] | {"code_requisition", "lifecycle", "current_version_id"},
    "resource": EDITABLE["resource"] | {"short_name", "email_address", "lifecycle"},
}


def _review_state(values: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "first_ingestion_id",
        "last_ingestion_id",
        "created_at",
        "updated_at",
        "occ_reviewed_by",
        "occ_reviewed_at",
        "row_hash",
    }
    return {k: v for k, v in values.items() if k not in metadata}


def snapshot(db: sqlite3.Connection, entity: str) -> dict[int, dict[str, Any]]:
    return {r[f"{entity}_id"]: dict(r) for r in db.execute(f"SELECT * FROM {entity}s")}


def record_changes(
    db: sqlite3.Connection, entity: str, before: dict[int, dict[str, Any]], run_id: int, now: str
) -> None:
    uploader = db.execute(
        "SELECT uploaded_by FROM ingestion_runs WHERE ingestion_run_id=?", (run_id,)
    ).fetchone()[0]
    for entity_id, after in snapshot(db, entity).items():
        old = before.get(entity_id)
        if old == after:
            continue
        change = (
            "New"
            if old is None
            else (
                "Removed"
                if old["lifecycle"] != after["lifecycle"]
                and after["lifecycle"] in {"closed", "off_bench"}
                else "Updated"
            )
        )
        pending = db.execute(
            "SELECT * FROM occ_approvals WHERE entity_type=? AND entity_id=? AND state='Pending'",
            (entity, entity_id),
        ).fetchone()
        if pending:
            original = json.loads(pending["before_json"]) if pending["before_json"] else None
            if original is not None and _review_state(original) == _review_state(after):
                db.execute("DELETE FROM occ_approvals WHERE entry_id=?", (pending["entry_id"],))
                db.execute(
                    f"UPDATE {entity}s SET occ_reviewed_by=?,occ_reviewed_at=? WHERE {entity}_id=?",
                    (original["occ_reviewed_by"], original["occ_reviewed_at"], entity_id),
                )
                continue
            change = "New" if pending["before_json"] is None else change
            db.execute(
                "UPDATE occ_approvals SET ingestion_run_id=?,after_json=?,change=?,conflict=1,"
                "uploaded_by=?,uploaded_at=? WHERE entry_id=?",
                (run_id, json.dumps(after), change, uploader, now, pending["entry_id"]),
            )
            continue
        modified = db.execute(
            "SELECT admin_values_json FROM occ_approvals WHERE entity_type=? AND entity_id=? "
            "AND state='Modified' ORDER BY entry_id DESC LIMIT 1",
            (entity, entity_id),
        ).fetchone()
        admin = json.loads(modified[0]) if modified else {}
        conflict = any(old and old.get(k) == v and after.get(k) != v for k, v in admin.items())
        db.execute(
            "INSERT INTO occ_approvals(entity_type,entity_id,ingestion_run_id,change,"
            "before_json,after_json,admin_values_json,conflict,uploaded_by,uploaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                entity,
                entity_id,
                run_id,
                change,
                json.dumps(old) if old else None,
                json.dumps(after),
                json.dumps(admin) if conflict else None,
                int(conflict),
                uploader,
                now,
            ),
        )


def _text(field: str, value: Any) -> str | None:
    if field == "email_address":
        return _mask(value)
    return str(value) if value is not None else None


def entry(db: sqlite3.Connection, row: sqlite3.Row) -> ApprovalEntry:
    before = json.loads(row["before_json"]) if row["before_json"] else {}
    after = json.loads(row["after_json"])
    admin = json.loads(row["admin_values_json"] or "{}")
    fields = []
    for field in sorted(DISPLAY[row["entity_type"]]):
        if before.get(field) != after.get(field) or field in admin:
            item = ApprovalField(
                field=to_camel(field),
                before=_text(field, before.get(field)),
                after=_text(field, after.get(field)),
            )
            if field in admin:
                item.admin_value = _text(field, admin[field])
            fields.append(item)
    user = db.execute(
        "SELECT display_name FROM users WHERE user_id=?", (row["uploaded_by"],)
    ).fetchone()
    key = (
        after["code_requisition"]
        if row["entity_type"] == "demand"
        else (f"{after['short_name']} · {_mask(after['email_address']) or 'No email'}")
    )
    return ApprovalEntry(
        entry_id=row["entry_id"],
        source="DL" if row["entity_type"] == "demand" else "CT",
        target_id=row["entity_id"],
        key=key,
        change=row["change"],
        uploaded_by=user[0] if user else "System",
        uploaded_at=row["uploaded_at"],
        state=row["state"],
        conflict=bool(row["conflict"]),
        fields=fields,
        comment=row["comment"],
    )


def list_entries(db: sqlite3.Connection) -> list[ApprovalEntry]:
    return [
        entry(db, r)
        for r in db.execute(
            "SELECT * FROM occ_approvals ORDER BY (state='Pending') DESC,"
            "uploaded_at DESC,entry_id DESC"
        ).fetchall()
    ]


def latest(
    db: sqlite3.Connection, entity: str, entity_id: int, pending: bool = False
) -> sqlite3.Row:
    if not db.execute(f"SELECT 1 FROM {entity}s WHERE {entity}_id=?", (entity_id,)).fetchone():
        raise Problem(404, "ENTITY_NOT_FOUND", "Demand or resource not found")
    row = db.execute(
        "SELECT * FROM occ_approvals WHERE entity_type=? AND entity_id=? "
        + ("AND state='Pending' " if pending else "")
        + "ORDER BY entry_id DESC LIMIT 1",
        (entity, entity_id),
    ).fetchone()
    if row is None:
        raise Problem(
            409 if pending else 404,
            "NO_PENDING_APPROVAL" if pending else "APPROVAL_NOT_FOUND",
            "No pending approval" if pending else "No approval history",
        )
    return row


def _eligibility(db: sqlite3.Connection, values: dict[str, Any]) -> None:
    config = domain_config(db)
    status = config.bench_statuses.resolve(values["bench_status"] or "")
    values["bench_status_code"] = status.code if status else None
    resource = Resource(
        resource_id=str(values["resource_id"]),
        short_name=values["short_name"],
        grade=values["grade"],
        location=values["location"],
        bench_status=values["bench_status"],
        group=values["group_name"],
        bench_start=_parsed(values["bench_start_date"]),
        bench_end=_parsed(values["bench_end_date"], values["bench_end_date_parse_status"]),
        ct_skills=values["skills_raw"],
    )
    reasons = eligibility(resource, [build_ct_basis(resource, config)], config)
    values["eligibility"] = "excluded" if reasons else "eligible"
    values["exclusion_reason"] = ",".join(r.code for r in reasons) or None


def _parsed(value: str | None, status: str | None = None) -> ParsedDate:
    if status == "unparseable":
        return ParsedDate(None, "unparseable", value)
    return ParsedDate(
        date.fromisoformat(value) if value else None, "valid" if value else "blank", value
    )


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"[\w.+-]+@[\w.-]+", "[email redacted]", value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    return value


def decide(
    db: sqlite3.Connection,
    entity: str,
    entity_id: int,
    decision: ApprovalDecision,
    user_id: int,
    now: str,
) -> ApprovalEntry:
    row = latest(db, entity, entity_id, pending=True)
    current = dict(
        db.execute(f"SELECT * FROM {entity}s WHERE {entity}_id=?", (entity_id,)).fetchone()
    )
    values = current.copy()
    admin: dict[str, Any] = {}
    if decision.action == "modify":
        allowed = {to_camel(k): k for k in EDITABLE[entity]}
        if set(decision.values) - allowed.keys():
            raise Problem(
                422, "FIELD_NOT_EDITABLE", "Only editable business fields may be modified"
            )
        for name, value in decision.values.items():
            field = allowed[name]
            if field.endswith("_date") and value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise Problem(422, "INVALID_DATE", "Dates must be ISO dates") from exc
            if (
                field in {"grade", "code_local_grade"}
                and value
                and not db.execute("SELECT 1 FROM grades WHERE code=?", (value,)).fetchone()
            ):
                raise Problem(422, "INVALID_GRADE", "Grade is not configured")
            if field == "available_capacity" and value not in {"", "Y", "N"}:
                raise Problem(422, "INVALID_CAPACITY", "Capacity must be Y or N")
            values[field] = value or None
            admin[field] = values[field]
        if entity == "resource":
            if "benchEndDate" in decision.values:
                values["bench_end_date_parse_status"] = (
                    "valid" if values["bench_end_date"] else "blank"
                )
            _eligibility(db, values)
        state = "Modified"
    elif decision.action == "revert":
        before = json.loads(row["before_json"]) if row["before_json"] else None
        if before is None:
            values["lifecycle"] = "closed" if entity == "demand" else "archived"
        else:
            values.update(before)
        state = "Reverted"
    else:
        state = "Approved"
    values.update(occ_reviewed_by=user_id, occ_reviewed_at=now, updated_at=now)
    changed = {k: v for k, v in values.items() if v != current[k]}
    if changed:
        assignments = ",".join(f"{k}=?" for k in changed)
        db.execute(
            f"UPDATE {entity}s SET {assignments} WHERE {entity}_id=?",
            (*changed.values(), entity_id),
        )
    db.execute(
        "UPDATE occ_approvals SET state=?,admin_values_json=?,comment=?,decided_by=?,"
        "decided_at=? WHERE entry_id=?",
        (
            state,
            json.dumps(admin) if admin else row["admin_values_json"],
            decision.comment,
            user_id,
            now,
            row["entry_id"],
        ),
    )
    audit_before = current
    audit_after = values
    audit_fields = set(changed)
    if decision.action == "approve":
        audit_before = json.loads(row["before_json"]) if row["before_json"] else {}
        audit_after = json.loads(row["after_json"])
        audit_fields = set(DISPLAY[entity])
    details = {
        to_camel(k): {"before": audit_before.get(k), "after": audit_after.get(k)}
        for k in audit_fields
        if k not in {"email_address", "email_key", "row_hash"}
        and audit_before.get(k) != audit_after.get(k)
    }
    append_audit(
        db,
        ts=now,
        action=f"OCC_{decision.action.upper()}",
        user_id=user_id,
        entity_type=entity,
        entity_id=str(entity_id),
        details={"fields": _redact(details), "comment": _redact(decision.comment)},
    )
    return entry(
        db,
        db.execute("SELECT * FROM occ_approvals WHERE entry_id=?", (row["entry_id"],)).fetchone(),
    )
