"""REQ-ING-01..03/06: persisted preview/discard/commit orchestration."""

import hashlib
import logging
import os
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException  # type: ignore[import-untyped]

from pfc.api.schemas.ingestion import IngestionRun, RunPage
from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.jobs.runner import JobRunner
from pfc.security.problems import Problem
from pfc.services.embeddings.chunking import OwnerType
from pfc.services.embeddings.incremental import submit_incremental_embed

from . import repository as repo
from .apply import apply_preview
from .limits import positive_setting
from .models import file_sha256
from .workbook_safety import WorkbookError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)


def commit_with_downstream(
    settings: Settings, job_runner: JobRunner, run_id: int, user_id: int, now: str
) -> IngestionRun:
    before = None
    try:
        with database(settings) as db:
            if repo.find_run(db, run_id)["kind"] == "DL":
                before = _current_versions(settings)
    except Exception:
        logger.warning("Cannot snapshot demands for incremental embedding")
    result = commit(settings, run_id, user_id, now)
    # CT skills are not chunked yet, so CT commits produce no embedding owners.
    if result.kind == "DL" and before is not None:
        try:
            after = _current_versions(settings)
            owners: set[tuple[OwnerType, int]] = set()
            for demand_id, current in after.items():
                previous = before.get(demand_id)
                if previous != current:
                    version = current[0]
                    old_version = previous[0] if previous is not None else None
                    for id in (old_version, version):
                        if id is not None:
                            owners.add(("jd", id))
                    if old_version is not None and old_version != version:
                        owners.update(
                            ("jd_item", id)
                            for id in _indexed_requirement_items(settings, old_version)
                        )
                    if version is not None and old_version != version:
                        owners.update(
                            ("jd_item", id)
                            for id in _confirmed_requirement_items(settings, version)
                        )
            if (
                owners
                and submit_incremental_embed(
                    job_runner,
                    settings,
                    sorted(owners),
                    created_by=user_id,
                )
                is None
            ):
                logger.warning("JD embedding not queued: no active embedding model")
        except Exception:
            logger.warning("JD embedding submission failed after ingestion commit")
    return result


def _current_versions(settings: Settings) -> dict[int, tuple[int | None, str | None]]:
    with database(settings) as db:
        return {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT demand_id,current_version_id,demand_request_name FROM demands"
            )
        }


def _confirmed_requirement_items(settings: Settings, version_id: int) -> set[int]:
    with database(settings) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT i.item_id FROM jd_requirement_items i "
                "JOIN jd_requirements r ON r.jd_req_id=i.jd_req_id "
                "WHERE r.demand_version_id=? AND r.status='confirmed'",
                (version_id,),
            )
        }


def _indexed_requirement_items(settings: Settings, version_id: int) -> set[int]:
    with database(settings) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT i.item_id FROM jd_requirement_items i "
                "JOIN jd_requirements r ON r.jd_req_id=i.jd_req_id "
                "JOIN embedding_chunks c ON c.owner_type='jd_item' AND c.owner_id=i.item_id "
                "JOIN embedding_models m ON m.embedding_model_id=c.embedding_model_id "
                "WHERE r.demand_version_id=? AND m.is_active=1",
                (version_id,),
            )
        }


def upload_limit(settings: Settings) -> int:
    with database(settings) as db:
        return max(
            1,
            int(
                positive_setting(db, "ingestion.max_upload_mb", MAX_UPLOAD_BYTES / 1024**2)
                * 1024**2
            ),
        )


def upload(
    settings: Settings,
    kind: Literal["DL", "CT"],
    name: str,
    data: bytes,
    user_id: int,
    now: str,
    source: Literal["manual_upload", "watch_folder"] = "manual_upload",
) -> IngestionRun:
    if Path(name).suffix.lower() != ".xlsx":
        raise Problem(422, "INVALID_WORKBOOK", "Only .xlsx workbooks are supported")
    digest = hashlib.sha256(data).hexdigest()
    directory = settings.data_dir / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    failure = (
        Problem(413, "UPLOAD_TOO_LARGE", "Workbook exceeds the upload size limit")
        if len(data) > upload_limit(settings)
        else None
    )
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        no_op = failure is None and (
            db.execute(
                "SELECT 1 FROM ingestion_runs WHERE kind=? AND file_sha256=? "
                "AND status='committed'",
                (kind, digest),
            ).fetchone()
            is not None
        )
        cursor = db.execute(
            "INSERT INTO ingestion_runs(kind, source, file_name, file_sha256, "
            "file_size, uploaded_by, status, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                kind,
                source,
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
        if not no_op:
            try:
                if failure is not None:
                    raise WorkbookError(
                        "UPLOAD_TOO_LARGE", "Workbook exceeds the upload limit", 413
                    )
                path.write_bytes(data)
                preview = repo.parse(db, path, kind)
                repo.save_review(db, settings, run_id, preview)
            except (ValueError, KeyError, TypeError, BadZipFile, InvalidFileException) as exc:
                failure = Problem(
                    exc.status if isinstance(exc, WorkbookError) else 422,
                    exc.code if isinstance(exc, WorkbookError) else "WORKBOOK_INVALID",
                    f"Workbook could not be parsed (run {run_id})",
                )
                db.execute(
                    "UPDATE ingestion_runs SET status='failed', error_summary=? "
                    "WHERE ingestion_run_id=?",
                    ("Workbook could not be parsed", run_id),
                )
        if failure is None:
            append_audit(
                db,
                ts=now,
                action="INGEST_NO_OP" if no_op else "UPLOAD_PREVIEW",
                user_id=user_id,
                entity_type="ingestion_run",
                entity_id=str(run_id),
                details={
                    "run_id": run_id,
                    "kind": kind,
                    **({"source": source} if source == "watch_folder" else {}),
                },
            )
        elif source == "watch_folder":
            append_audit(
                db,
                ts=now,
                action="INGEST_FAILED",
                user_id=user_id,
                entity_type="ingestion_run",
                entity_id=str(run_id),
                details={"run_id": run_id, "kind": kind, "source": source},
            )
        result = repo.run_response(db, settings, run_id)
    if failure is not None:
        path.unlink(missing_ok=True)
        repo.snapshot_path(settings, run_id).unlink(missing_ok=True)
        raise failure
    return result


def commit(settings: Settings, run_id: int, user_id: int, now: str) -> IngestionRun:
    staged = repo.staged_snapshot_path(settings, run_id)
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        run = repo.find_run(db, run_id)
        if run["status"] != "preview":
            raise Problem(409, "INGESTION_STATE", "Only a preview can be committed")
        try:
            path = repo.upload_path(settings, run_id)
            if not path.is_file() or file_sha256(path) != run["file_sha256"]:
                raise Problem(409, "UPLOAD_HASH_MISMATCH", "Stored workbook is missing or changed")
            try:
                preview = repo.parse(db, path, run["kind"])
            except (ValueError, KeyError, TypeError, BadZipFile, InvalidFileException) as exc:
                raise Problem(
                    422,
                    exc.code if isinstance(exc, WorkbookError) else "WORKBOOK_INVALID",
                    "Stored workbook could not be parsed",
                ) from exc
            snapshot = repo.build_review(db, preview)
            staged.write_text(snapshot.model_dump_json(), encoding="utf-8")
            repo.save_review(db, settings, run_id, preview, write_snapshot=False)
            apply_preview(db, preview, run_id, now)
            db.execute(
                "UPDATE ingestion_runs SET status='committed', committed_at=? "
                "WHERE ingestion_run_id=?",
                (now, run_id),
            )
            append_audit(
                db,
                ts=now,
                action="INGEST_COMMIT",
                user_id=user_id,
                entity_type="ingestion_run",
                entity_id=str(run_id),
                details={
                    "run_id": run_id,
                    **snapshot.counts.model_dump(),
                    **({"source": "watch_folder"} if run["source"] == "watch_folder" else {}),
                },
            )
            db.commit()
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, repo.snapshot_path(settings, run_id))
    except OSError:
        logger.warning(
            "Snapshot promotion failed for ingestion run %s; using staged snapshot", run_id
        )
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
            details={
                "run_id": run_id,
                **({"source": "watch_folder"} if run["source"] == "watch_folder" else {}),
            },
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
