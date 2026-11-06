"""REQ-ING-01..03/06: persisted preview/discard/commit orchestration."""

import hashlib
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException  # type: ignore[import-untyped]

from pfc.api.schemas.ingestion import IngestionRun, RunPage
from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.security.problems import Problem

from . import repository as repo
from .apply import apply_preview
from .models import file_sha256

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def upload(
    settings: Settings, kind: Literal["DL", "CT"], name: str, data: bytes, user_id: int, now: str
) -> IngestionRun:
    if len(data) > MAX_UPLOAD_BYTES:
        raise Problem(413, "UPLOAD_TOO_LARGE", "Workbook exceeds 20 MB limit")
    if Path(name).suffix.lower() != ".xlsx":
        raise Problem(422, "INVALID_WORKBOOK", "Only .xlsx workbooks are supported")
    digest = hashlib.sha256(data).hexdigest()
    directory = settings.data_dir / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    failed = False
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        no_op = (
            db.execute(
                "SELECT 1 FROM ingestion_runs WHERE kind=? AND file_sha256=? "
                "AND status='committed'",
                (kind, digest),
            ).fetchone()
            is not None
        )
        cursor = db.execute(
            "INSERT INTO ingestion_runs(kind, source, file_name, file_sha256, "
            "file_size, uploaded_by, status, started_at) VALUES (?,'manual_upload',?,?,?,?,?,?)",
            (
                kind,
                Path(name).name,
                digest,
                len(data),
                user_id,
                "no_op" if no_op else "preview",
                now,
            ),
        )
        assert cursor.lastrowid is not None
        run_id = cursor.lastrowid
        path = repo.upload_path(settings, run_id)
        path.write_bytes(data)
        if not no_op:
            try:
                preview = repo.parse(db, path, kind)
                repo.save_review(db, settings, run_id, preview)
            except (ValueError, BadZipFile, InvalidFileException):
                failed = True
                db.execute(
                    "UPDATE ingestion_runs SET status='failed', error_summary=? "
                    "WHERE ingestion_run_id=?",
                    ("Workbook could not be parsed", run_id),
                )
        if not failed:
            append_audit(
                db,
                ts=now,
                action="INGEST_NO_OP" if no_op else "UPLOAD_PREVIEW",
                user_id=user_id,
                entity_type="ingestion_run",
                entity_id=str(run_id),
                details={"run_id": run_id, "kind": kind},
            )
        result = repo.run_response(db, settings, run_id)
    if failed:
        raise Problem(422, "INVALID_WORKBOOK", f"Workbook could not be parsed (run {run_id})")
    return result


def commit(settings: Settings, run_id: int, user_id: int, now: str) -> IngestionRun:
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        run = repo.find_run(db, run_id)
        if run["status"] != "preview":
            raise Problem(409, "INGESTION_STATE", "Only a preview can be committed")
        path = repo.upload_path(settings, run_id)
        if not path.is_file() or file_sha256(path) != run["file_sha256"]:
            raise Problem(409, "UPLOAD_HASH_MISMATCH", "Stored workbook is missing or changed")
        try:
            preview = repo.parse(db, path, run["kind"])
        except (ValueError, BadZipFile, InvalidFileException) as exc:
            raise Problem(422, "INVALID_WORKBOOK", "Stored workbook could not be parsed") from exc
        snapshot = repo.save_review(db, settings, run_id, preview, write_snapshot=False)
        apply_preview(db, preview, run_id, now)
        db.execute(
            "UPDATE ingestion_runs SET status='committed', committed_at=? WHERE ingestion_run_id=?",
            (now, run_id),
        )
        append_audit(
            db,
            ts=now,
            action="INGEST_COMMIT",
            user_id=user_id,
            entity_type="ingestion_run",
            entity_id=str(run_id),
            details={"run_id": run_id, **snapshot.counts.model_dump()},
        )
    repo.snapshot_path(settings, run_id).write_text(snapshot.model_dump_json(), encoding="utf-8")
    with database(settings) as db:
        return repo.run_response(db, settings, run_id)


def discard(settings: Settings, run_id: int, user_id: int, now: str) -> IngestionRun:
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        run = repo.find_run(db, run_id)
        if run["status"] != "preview":
            raise Problem(409, "INGESTION_STATE", "Only a preview can be discarded")
        db.execute(
            "UPDATE ingestion_runs SET status='discarded' WHERE ingestion_run_id=?", (run_id,)
        )
        append_audit(
            db,
            ts=now,
            action="INGEST_DISCARD",
            user_id=user_id,
            entity_type="ingestion_run",
            entity_id=str(run_id),
            details={"run_id": run_id},
        )
        result = repo.run_response(db, settings, run_id)
    repo.upload_path(settings, run_id).unlink(missing_ok=True)
    return result


def history(settings: Settings, limit: int, offset: int) -> RunPage:
    with database(settings) as db:
        total = db.execute(
            "SELECT count(*) FROM ingestion_runs WHERE kind IN ('DL','CT')"
        ).fetchone()[0]
        ids = db.execute(
            "SELECT ingestion_run_id FROM ingestion_runs WHERE kind IN ('DL','CT') "
            "ORDER BY ingestion_run_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return RunPage(
            items=[repo.run_response(db, settings, r[0]) for r in ids],
            total=total,
            limit=limit,
            offset=offset,
        )
