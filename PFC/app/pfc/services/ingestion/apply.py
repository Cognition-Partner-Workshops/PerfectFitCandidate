"""REQ-ING-03 / REQ-IN-02/11: transactional application of reviewed workbooks."""

import hashlib
import sqlite3
from collections import Counter
from datetime import date
from typing import Any

from pfc.domain.config_loader import DomainConfig
from pfc.domain.normalise import ParsedDate
from pfc.domain.scoring.engine import build_ct_basis, eligibility
from pfc.domain.scoring.models import Resource
from pfc.security.problems import Problem
from pfc.services.candidates.classification import reclassify_all

from .approvals import record_changes, snapshot
from .eligibility import refresh_resource_eligibility
from .models import ParsedRow, Preview
from .repository import domain_config
from .workbooks import CT, DL


def _upsert(
    db: sqlite3.Connection,
    table: str,
    pk: str,
    keys: dict[str, Any],
    values: dict[str, Any],
    run_id: int,
    now: str,
) -> int:
    where = " AND ".join(f"{k}=?" for k in keys)
    old = db.execute(f"SELECT * FROM {table} WHERE {where}", tuple(keys.values())).fetchone()
    if old and (
        table == "demands"
        and old["row_hash"] == values["row_hash"]
        and old["lifecycle"] == values["lifecycle"]
        or all(old[field] == value for field, value in values.items())
    ):
        return int(old[pk])
    values.update(
        last_ingestion_id=run_id, updated_at=now, occ_reviewed_by=None, occ_reviewed_at=None
    )
    if old:
        assignments = ", ".join(f"{k}=?" for k in values)
        db.execute(f"UPDATE {table} SET {assignments} WHERE {pk}=?", (*values.values(), old[pk]))
        return int(old[pk])
    values.update(first_ingestion_id=run_id, created_at=now)
    names = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = db.execute(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(values.values())
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _candidate(db: sqlite3.Connection, row: ParsedRow, now: str) -> int:
    f = row.fields
    if f["email_key"]:
        matches = db.execute(
            "SELECT candidate_id FROM candidate_emails WHERE email_key=?", (f["email_key"],)
        ).fetchall()
        if len(matches) > 1:
            raise Problem(409, "AMBIGUOUS_IDENTITY", "Email identity needs human confirmation")
        if matches:
            return int(matches[0][0])
    old = db.execute(
        "SELECT candidate_id FROM resources WHERE short_name_key=? AND email_key=?",
        (f["short_name_key"], f["email_key"]),
    ).fetchone()
    if old and old[0] is not None:
        return int(old[0])
    candidate_id = db.execute("SELECT coalesce(max(candidate_id),0)+1 FROM candidates").fetchone()[
        0
    ]
    db.execute(
        "INSERT INTO candidates(candidate_id, internal_ref, display_name, identity_status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (
            candidate_id,
            f"PFC-C-{candidate_id:06d}",
            f["short_name"],
            "complete" if f["email_key"] else "incomplete",
            now,
            now,
        ),
    )
    if f["email_key"]:
        db.execute(
            "INSERT INTO candidate_emails(candidate_id, email, email_key, source) "
            "VALUES (?,?,?,'CT')",
            (candidate_id, f["email_address"], f["email_key"]),
        )
    return int(candidate_id)


def _parsed(value: str | None, status: str | None = None) -> ParsedDate:
    if status == "unparseable":
        return ParsedDate(None, "unparseable", None)
    return ParsedDate(
        date.fromisoformat(value) if value else None, "valid" if value else "blank", value
    )


def _resource(
    db: sqlite3.Connection, row: ParsedRow, config: DomainConfig, run_id: int, now: str
) -> None:
    f = row.fields
    values = {field: f.get(field) for field in CT.values()}
    values.update(
        short_name_key=f["short_name_key"],
        email_key=f["email_key"],
        bench_end_date_parse_status=f["bench_end_date_parse_status"],
        row_hash=row.row_hash,
        lifecycle="active",
    )
    old = db.execute(
        "SELECT * FROM resources WHERE short_name_key=? AND email_key=?",
        (f["short_name_key"], f["email_key"]),
    ).fetchone()
    unchanged = old is not None and old["row_hash"] == row.row_hash and old["lifecycle"] == "active"
    if unchanged:
        values = {field: old[field] for field in values}
    if not db.execute("SELECT 1 FROM grades WHERE code=?", (values["grade"],)).fetchone():
        values["grade"] = None
    status = config.bench_statuses.resolve(values["bench_status"] or "")
    values["bench_status_code"] = status.code if status else None
    resource = Resource(
        resource_id=str(row.row_number),
        short_name=f["short_name"],
        grade=values["grade"],
        location=values["location"],
        bench_status=values["bench_status"],
        group=values["group_name"],
        bench_start=_parsed(values["bench_start_date"]),
        bench_end=_parsed(values["bench_end_date"], values["bench_end_date_parse_status"]),
        ct_skills=values["skills_raw"],
    )
    reasons = eligibility(resource, [build_ct_basis(resource, config)], config)
    values.update(
        eligibility="excluded" if reasons else "eligible",
        exclusion_reason=",".join(r.code for r in reasons) or None,
        candidate_id=old["candidate_id"] if unchanged else _candidate(db, row, now),
    )
    _upsert(
        db,
        "resources",
        "resource_id",
        {"short_name_key": f["short_name_key"], "email_key": f["email_key"]},
        values,
        run_id,
        now,
    )


def _demand(
    db: sqlite3.Connection, row: ParsedRow, config: DomainConfig, run_id: int, now: str
) -> None:
    f = row.fields
    values = {field: f.get(field) for field in DL.values() if field != "jd_text_norm"}
    values.update(id_format_ok=f["id_format_ok"], row_hash=row.row_hash, lifecycle="open")
    if not db.execute(
        "SELECT 1 FROM grades WHERE code=?", (values["code_local_grade"],)
    ).fetchone():
        values["code_local_grade"] = None
    demand_id = _upsert(
        db, "demands", "demand_id", {"code_requisition": f["code_requisition"]}, values, run_id, now
    )
    digest = hashlib.sha256(f["jd_text_norm"].encode()).hexdigest()
    version = db.execute(
        "SELECT demand_version_id FROM demand_versions WHERE demand_id=? AND jd_sha256=?",
        (demand_id, digest),
    ).fetchone()
    if version:
        version_id = version[0]
    else:
        number = db.execute(
            "SELECT coalesce(max(version_no),0)+1 FROM demand_versions WHERE demand_id=?",
            (demand_id,),
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO demand_versions(demand_id, version_no, jd_text_raw, "
            "jd_text_norm, jd_sha256, ingestion_run_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (demand_id, number, row.jd_text_raw or "", f["jd_text_norm"], digest, run_id, now),
        )
        version_id = cursor.lastrowid
    db.execute("UPDATE demands SET current_version_id=? WHERE demand_id=?", (version_id, demand_id))


def apply_preview(db: sqlite3.Connection, preview: Preview, run_id: int, now: str) -> None:
    entity = "demand" if preview.kind == "DL" else "resource"
    before = snapshot(db, entity)
    config = domain_config(db)
    accepted = [r for r in preview.rows if not r.rejected and not r.superseded]
    incomplete_counts = Counter(
        r.fields["short_name_key"]
        for r in accepted
        if preview.kind == "CT" and not r.fields["email_key"]
    )
    present = {r.key for r in accepted}
    for row in accepted:
        if preview.kind == "DL":
            _demand(db, row, config, run_id, now)
        elif row.fields["email_key"] or incomplete_counts[row.fields["short_name_key"]] == 1:
            _resource(db, row, config, run_id, now)
    if preview.kind == "DL":
        for record in db.execute(
            "SELECT demand_id, code_requisition FROM demands WHERE lifecycle='open'"
        ).fetchall():
            if record["code_requisition"] not in present:
                db.execute(
                    "UPDATE demands SET lifecycle='closed', updated_at=?, last_ingestion_id=?, "
                    "occ_reviewed_by=NULL, occ_reviewed_at=NULL WHERE demand_id=?",
                    (now, run_id, record[0]),
                )
    else:
        for record in db.execute(
            "SELECT resource_id, short_name_key, email_key FROM resources "
            "WHERE lifecycle='active' AND email_key<>''"
        ).fetchall():
            if (record["short_name_key"], record["email_key"]) not in present:
                db.execute(
                    "UPDATE resources SET lifecycle='off_bench', updated_at=?, "
                    "last_ingestion_id=?, "
                    "occ_reviewed_by=NULL, occ_reviewed_at=NULL WHERE resource_id=?",
                    (now, run_id, record[0]),
                )
    record_changes(db, entity, before, run_id, now)
    if preview.kind == "CT":
        reclassify_all(db)
        refresh_resource_eligibility(db)
