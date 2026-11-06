"""REQ-ING-02/06: run persistence; all writes use the caller's transaction."""

import json
import sqlite3
from pathlib import Path
from typing import Literal

from pfc.api.schemas.ingestion import IngestionRun, IssuePage, ReviewSnapshot, RunCounts
from pfc.config import Settings
from pfc.domain.config_loader import (
    BenchStatus,
    BenchStatuses,
    DomainConfig,
    GradeLadder,
    load_domain_config,
)
from pfc.domain.normalise import normalise_term
from pfc.security.problems import Problem

from .models import Diff, Preview, RowIssue, RowKey
from .workbooks import parse_workbook

ISSUE_CAP = 100


def upload_path(settings: Settings, run_id: int) -> Path:
    return settings.data_dir / "uploads" / f"{run_id}.xlsx"


def snapshot_path(settings: Settings, run_id: int) -> Path:
    return upload_path(settings, run_id).with_suffix(".json")


def find_run(db: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM ingestion_runs WHERE ingestion_run_id=?", (run_id,)).fetchone()
    if row is None:
        raise Problem(404, "INGESTION_NOT_FOUND", "Ingestion run not found")
    return row


def existing_hashes(db: sqlite3.Connection, kind: str) -> dict[RowKey, str]:
    if kind == "DL":
        return {
            r[0]: r[1]
            for r in db.execute(
                "SELECT code_requisition, row_hash FROM demands WHERE lifecycle='open'"
            )
        }
    return {
        (r[0], r[1]): r[2]
        for r in db.execute(
            "SELECT short_name_key, email_key, row_hash FROM resources WHERE lifecycle='active'"
        )
    }


def domain_config(db: sqlite3.Connection) -> DomainConfig:
    """Use OCC-editable DB reference data with domain skill/template defaults."""
    defaults = load_domain_config()
    statuses = {
        r["code"]: BenchStatus(**dict(r)) for r in db.execute("SELECT * FROM bench_statuses")
    }
    mapping = {}
    for row in db.execute("SELECT * FROM status_map"):
        canonical = statuses.get(row["canonical_code"])
        if canonical:
            source = normalise_term(row["source_value"])
            key = f"source:{source}"
            statuses[key] = canonical.model_copy(
                update={"is_excluded": bool(row["is_excluded"]), "priority": row["priority"]}
            )
            mapping[source] = key
    codes = tuple(r[0] for r in db.execute("SELECT code FROM grades ORDER BY ordinal"))
    return defaults.model_copy(
        update={
            "grades": GradeLadder(codes=codes) if codes else defaults.grades,
            "bench_statuses": BenchStatuses(statuses=statuses, status_map=mapping),
        }
    )


def parse(db: sqlite3.Connection, path: Path, kind: Literal["DL", "CT"]) -> Preview:
    config = domain_config(db)
    return parse_workbook(
        path,
        kind,
        grades=frozenset(r[0] for r in db.execute("SELECT code FROM grades")),
        known_statuses=frozenset(config.bench_statuses.status_map),
    )


def save_review(
    db: sqlite3.Connection, settings: Settings, run_id: int, preview: Preview
) -> ReviewSnapshot:
    diff = preview.diff(existing_hashes(db, preview.kind))
    counts = RunCounts(
        rows_read=preview.rows_read,
        rows_new=len(diff.new),
        rows_updated=len(diff.changed),
        rows_unchanged=len(diff.unchanged),
        rows_rejected=diff.rejected,
        rows_incomplete=len(diff.incomplete),
    )
    snapshot = ReviewSnapshot(diff=diff, counts=counts)
    snapshot_path(settings, run_id).write_text(snapshot.model_dump_json(), encoding="utf-8")
    db.execute(
        "UPDATE ingestion_runs SET sheet_name=?, header_row=?, rows_read=?, rows_new=?, "
        "rows_updated=?, rows_unchanged=?, rows_rejected=? WHERE ingestion_run_id=?",
        (
            preview.sheet_name,
            preview.header_row,
            counts.rows_read,
            counts.rows_new,
            counts.rows_updated,
            counts.rows_unchanged,
            counts.rows_rejected,
            run_id,
        ),
    )
    db.execute("DELETE FROM ingestion_row_issues WHERE ingestion_run_id=?", (run_id,))
    db.executemany(
        "INSERT INTO ingestion_row_issues "
        "(ingestion_run_id, row_number, field, severity, code, message, raw_value) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (
                run_id,
                i.row_number,
                i.column,
                i.severity,
                i.code,
                i.message,
                json.dumps(i.model_dump(mode="json")["raw_value"], ensure_ascii=False),
            )
            for i in preview.issues
        ],
    )
    return snapshot


def issues(db: sqlite3.Connection, run_id: int, limit: int, offset: int) -> IssuePage:
    find_run(db, run_id)
    total = db.execute(
        "SELECT count(*) FROM ingestion_row_issues WHERE ingestion_run_id=?", (run_id,)
    ).fetchone()[0]
    items = [
        RowIssue(
            row_number=r["row_number"],
            column=r["field"],
            severity=r["severity"],
            code=r["code"],
            message=r["message"],
            raw_value=json.loads(r["raw_value"]) if r["raw_value"] else None,
        )
        for r in db.execute(
            "SELECT * FROM ingestion_row_issues WHERE ingestion_run_id=? "
            "ORDER BY issue_id LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        )
    ]
    return IssuePage(items=items, total=total, limit=limit, offset=offset)


def run_response(db: sqlite3.Connection, settings: Settings, run_id: int) -> IngestionRun:
    row = find_run(db, run_id)
    path = snapshot_path(settings, run_id)
    snapshot = (
        ReviewSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        if path.exists()
        else None
    )
    page = issues(db, run_id, ISSUE_CAP, 0)
    return IngestionRun(
        **{
            k: row[k]
            for k in (
                "ingestion_run_id",
                "kind",
                "status",
                "file_name",
                "file_sha256",
                "file_size",
                "sheet_name",
                "header_row",
                "started_at",
                "committed_at",
                "error_summary",
            )
        },
        no_op=row["status"] == "no_op",
        counts=snapshot.counts if snapshot else RunCounts(),
        diff=snapshot.diff if snapshot else Diff(),
        issues=page.items,
        issues_total=page.total,
    )
