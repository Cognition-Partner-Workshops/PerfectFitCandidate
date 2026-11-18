"""Explicit, fingerprinted archive and retention batches."""

import calendar
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.domain.scoring.engine import build_ct_basis, eligibility
from pfc.jobs.runner import JobContext, JobRunner
from pfc.security.problems import Problem
from pfc.services.ingestion.repository import domain_config
from pfc.services.lifecycle import entity_table, record_transition
from pfc.services.matching.common import atomic, digest
from pfc.services.matching.cv_bases import load_resources
from pfc.services.retention import purge_entities


def months_before(value: datetime, months: int) -> datetime:
    year, month = divmod(value.year * 12 + value.month - 1 - months, 12)
    return value.replace(
        year=year, month=month + 1, day=min(value.day, calendar.monthrange(year, month + 1)[1])
    )


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def setting(db: sqlite3.Connection, key: str, default: int) -> int:
    row = db.execute(
        "SELECT value_json FROM settings WHERE scope='system' AND key=?", (key,)
    ).fetchone()
    return int(json.loads(row[0])) if row else default


def fingerprint(row: sqlite3.Row) -> str:
    return digest(dict(row))


def batch(db: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM archive_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row is None:
        raise Problem(404, "BATCH_NOT_FOUND", "Archive batch not found")
    result = dict(row)
    for field in ("criteria_json", "preview_json", "result_json"):
        result[field.removesuffix("_json")] = json.loads(result.pop(field) or "null")
    result["items"] = [
        dict(item)
        for item in db.execute(
            "SELECT * FROM archive_batch_items WHERE batch_id=? ORDER BY entity_type,entity_id",
            (batch_id,),
        )
    ]
    return result


def selection_hash(db: sqlite3.Connection, batch_id: int) -> str:
    return digest(
        [
            tuple(row)
            for row in db.execute(
                "SELECT entity_type,entity_id,fingerprint,included FROM archive_batch_items "
                "WHERE batch_id=? ORDER BY entity_type,entity_id",
                (batch_id,),
            )
        ]
    )


def preview(
    db: sqlite3.Connection,
    *,
    kind: str,
    user_id: int,
    now: datetime,
    ended_before: str | None = None,
) -> dict[str, Any]:
    with atomic(db, immediate=True):
        db.execute(
            "UPDATE archive_batches SET status='failed' WHERE status='running' "
            "AND job_id IN (SELECT job_id FROM jobs WHERE status IN ('failed','cancelled'))"
        )
        if db.execute("SELECT 1 FROM archive_batches WHERE status='running'").fetchone():
            raise Problem(409, "BATCH_RUNNING", "Wait for the running archive or purge batch")
        db.execute("UPDATE archive_batches SET status='expired' WHERE status='previewed'")
        cutoff = months_before(now, setting(db, "archive.retention_months", 12)).isoformat()
        cursor = db.execute(
            "INSERT INTO archive_batches(kind,trigger,status,criteria_json,created_by,created_at) "
            "VALUES (?,'manual','previewed',?,?,?)",
            (
                kind,
                json.dumps({"ended_before": ended_before, "retention_cutoff": cutoff}),
                user_id,
                now.isoformat(),
            ),
        )
        batch_id = int(cursor.lastrowid or 0)
        config = domain_config(db)
        resource_reasons = (
            {
                int(resource.resource_id): {
                    r.code
                    for r in eligibility(resource, [build_ct_basis(resource, config)], config)
                }
                for resource in load_resources(db)
            }
            if kind == "archive"
            else {}
        )
        counts = {"demand": 0, "resource": 0}
        for entity in counts:
            for row in db.execute(f"SELECT * FROM {entity}s").fetchall():
                reason = None
                if kind == "purge":
                    if (
                        row["lifecycle"] == "archived"
                        and row["archived_at"]
                        and (timestamp(row["archived_at"]) < timestamp(cutoff))
                    ):
                        reason = "retention_expired"
                elif row["lifecycle"] == ("closed" if entity == "demand" else "off_bench"):
                    reason = "ended"
                elif entity == "resource" and row["lifecycle"] == "active":
                    reasons = resource_reasons.get(row["resource_id"], set())
                    status = config.bench_statuses.resolve(row["bench_status"] or "")
                    selected = status is not None and status.code == "SELECTED"
                    reason = (
                        "selected"
                        if selected
                        else "not_on_bench"
                        if "NOT_ON_BENCH" in reasons
                        else "bench_end_set"
                        if "BENCH_END_SET" in reasons
                        else None
                    )
                if reason is None or (
                    ended_before and (not row["ended_at"] or row["ended_at"][:10] >= ended_before)
                ):
                    continue
                entity_id = row[f"{entity}_id"]
                label = f"{entity.capitalize()} {entity_id}"
                db.execute(
                    "INSERT INTO archive_batch_items(batch_id,entity_type,entity_id,label,reason,"
                    "fingerprint) VALUES (?,?,?,?,?,?)",
                    (batch_id, entity, entity_id, label, reason, fingerprint(row)),
                )
                counts[entity] += 1
        db.execute(
            "UPDATE archive_batches SET preview_json=?,preview_hash=? WHERE batch_id=?",
            (json.dumps(counts), selection_hash(db, batch_id), batch_id),
        )
        return batch(db, batch_id)


def select_items(
    db: sqlite3.Connection, batch_id: int, items: list[dict[str, Any]]
) -> dict[str, Any]:
    with atomic(db, immediate=True):
        current = batch(db, batch_id)
        if current["status"] != "previewed":
            raise Problem(409, "BATCH_NOT_PREVIEWED", "Build a new preview")
        for item in items:
            cursor = db.execute(
                "UPDATE archive_batch_items SET included=? WHERE batch_id=? AND entity_type=? "
                "AND entity_id=?",
                (int(item["included"]), batch_id, item["entity_type"], item["entity_id"]),
            )
            if not cursor.rowcount:
                raise Problem(404, "ITEM_NOT_FOUND", "Preview item not found")
        db.execute(
            "UPDATE archive_batches SET preview_hash=? WHERE batch_id=?",
            (selection_hash(db, batch_id), batch_id),
        )
        return batch(db, batch_id)


def execute_batch(settings: Settings, batch_id: int, user_id: int, now: datetime) -> dict[str, int]:
    try:
        with database(settings) as db, atomic(db, immediate=True):
            current = batch(db, batch_id)
            if current["status"] != "running":
                raise Problem(409, "BATCH_NOT_RUNNING", "Batch is not running")
            included = [item for item in current["items"] if item["included"]]
            for item in included:
                entity = item["entity_type"]
                row = db.execute(
                    f"SELECT * FROM {entity_table(entity)} WHERE {entity}_id=?",
                    (item["entity_id"],),
                ).fetchone()
                if row is None or fingerprint(row) != item["fingerprint"]:
                    raise Problem(
                        409, "PREVIEW_DRIFT", "Data changed since preview — rebuild preview"
                    )
            counts = {"demand": 0, "resource": 0}
            if current["kind"] == "purge":
                purge_entities(
                    db, included, user_id=user_id, batch_id=batch_id, now=now.isoformat()
                )
            for item in included:
                entity, entity_id = item["entity_type"], item["entity_id"]
                if current["kind"] == "archive":
                    state = db.execute(
                        f"SELECT lifecycle FROM {entity}s WHERE {entity}_id=?", (entity_id,)
                    ).fetchone()[0]
                    record_transition(
                        db,
                        entity,
                        entity_id,
                        state,
                        "archived",
                        "archive",
                        now=now.isoformat(),
                        user_id=user_id,
                        archive_batch_id=batch_id,
                        metadata={"archived_at": now.isoformat(), "archive_batch_id": batch_id},
                    )
                db.execute(
                    "UPDATE archive_batch_items SET outcome=? WHERE batch_id=? "
                    "AND entity_type=? AND entity_id=?",
                    (
                        "archived" if current["kind"] == "archive" else "purged",
                        batch_id,
                        entity,
                        entity_id,
                    ),
                )
                counts[entity] += 1
            db.execute(
                "UPDATE archive_batch_items SET outcome='skipped' WHERE batch_id=? AND included=0",
                (batch_id,),
            )
            db.execute(
                "UPDATE archive_batches SET status='completed',completed_at=?,result_json=? "
                "WHERE batch_id=?",
                (now.isoformat(), json.dumps(counts), batch_id),
            )
            append_audit(
                db,
                ts=now.isoformat(),
                user_id=user_id,
                action="ARCHIVE_RUN" if current["kind"] == "archive" else "ARCHIVE_PURGE",
                entity_type="archive_batch",
                entity_id=str(batch_id),
                details=counts,
            )
            return counts
    except Exception:
        with database(settings) as db:
            db.execute(
                "UPDATE archive_batches SET status='failed',completed_at=?,result_json=? "
                "WHERE batch_id=? AND status='running'",
                (
                    now.isoformat(),
                    json.dumps({"error": "Data changed or operation failed; rebuild preview"}),
                    batch_id,
                ),
            )
        raise


def submit(
    settings: Settings,
    runner: JobRunner,
    batch_id: int,
    preview_hash: str,
    user_id: int,
    now: datetime,
    *,
    kind: str,
) -> int:
    with database(settings) as db, atomic(db, immediate=True):
        current = batch(db, batch_id)
        if (
            current["kind"] != kind
            or current["status"] != "previewed"
            or current["preview_hash"] != preview_hash
        ):
            raise Problem(409, "PREVIEW_CONFLICT", "Preview changed; reload the batch")
        if not any(item["included"] for item in current["items"]):
            raise Problem(422, "EMPTY_SELECTION", "Select at least one preview item")
        db.execute("UPDATE archive_batches SET status='running' WHERE batch_id=?", (batch_id,))

    def run(context: JobContext) -> None:
        counts = execute_batch(settings, batch_id, user_id, now)
        context.report_progress(1, json.dumps(counts))

    try:
        job_id = runner.submit(
            "archive" if kind == "archive" else "archive_purge",
            run,
            params={"batch_id": batch_id},
            created_by=user_id,
        )
    except Exception:
        with database(settings) as db:
            db.execute("UPDATE archive_batches SET status='failed' WHERE batch_id=?", (batch_id,))
        raise
    with database(settings) as db:
        db.execute("UPDATE archive_batches SET job_id=? WHERE batch_id=?", (job_id, batch_id))
    return job_id
