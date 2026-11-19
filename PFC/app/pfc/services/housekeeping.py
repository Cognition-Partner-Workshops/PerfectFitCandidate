"""Conservative data-directory cleanup, explicit backups and vector repair."""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.services.archival import setting
from pfc.services.retention import delete_chunks, vector_tables


def orphan_chunks(db: sqlite3.Connection) -> list[int]:
    owners = {
        "jd": ("demand_versions", "demand_version_id"),
        "jd_item": ("jd_requirement_items", "item_id"),
        "cv": ("cv_versions", "cv_version_id"),
        "ct_skills": ("resources", "resource_id"),
        "skill": ("skills", "skill_id"),
    }
    reembedding = db.execute(
        "SELECT 1 FROM jobs WHERE kind='embed' AND status IN ('queued','running')"
    ).fetchone()
    result = []
    archived_owners = {
        "jd": "SELECT v.demand_version_id FROM demand_versions v JOIN demands d USING(demand_id) "
        "WHERE d.lifecycle='archived'",
        "jd_item": "SELECT i.item_id FROM jd_requirement_items i "
        "JOIN jd_requirements q USING(jd_req_id) JOIN demand_versions v USING(demand_version_id) "
        "JOIN demands d USING(demand_id) WHERE d.lifecycle='archived'",
        "ct_skills": "SELECT resource_id FROM resources WHERE lifecycle='archived'",
        "cv": "SELECT v.cv_version_id FROM cv_versions v JOIN resources r USING(candidate_id) "
        "WHERE r.lifecycle='archived'",
    }
    archived = {kind: {r[0] for r in db.execute(query)} for kind, query in archived_owners.items()}
    for row in db.execute(
        "SELECT c.*,m.is_active FROM embedding_chunks c "
        "JOIN embedding_models m USING(embedding_model_id)"
    ):
        if row["owner_id"] in archived.get(row["owner_type"], set()):
            continue
        table, pk = owners[row["owner_type"]]
        exists = db.execute(f"SELECT 1 FROM {table} WHERE {pk}=?", (row["owner_id"],)).fetchone()
        if not exists or (not row["is_active"] and not reembedding):
            result.append(row["chunk_id"])
    return result


def orphan_vectors(db: sqlite3.Connection) -> dict[str, list[int]]:
    return {
        name: [
            row[0]
            for row in db.execute(
                f'SELECT rowid FROM "{name}" '
                "WHERE rowid NOT IN (SELECT chunk_id FROM embedding_chunks)"
            )
        ]
        for name in vector_tables(db)
    }


def expired_files(settings: Settings, now: datetime, *, logs: bool, days: int) -> list[Path]:
    root = settings.data_dir.resolve()
    directories = (
        [root / "logs"] if logs else [root / "temp", root / "tmp", root / "work" / "cv-extraction"]
    )
    active = {
        Path(handler.baseFilename).resolve()
        for logger in [
            logging.getLogger(),
            *[
                value
                for value in logging.Logger.manager.loggerDict.values()
                if isinstance(value, logging.Logger)
            ],
        ]
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    }
    result = []
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if (
                not path.is_file()
                or path.is_symlink()
                or any(p.is_symlink() for p in path.parents if p != root)
            ):
                continue
            if not path.resolve().is_relative_to(root) or path.resolve() in active:
                continue
            if logs and path.name in {"pfc.log", "llm.log", "llama-server.log"}:
                continue
            if now.timestamp() - path.stat().st_mtime > days * 86400:
                result.append(path)
    return sorted(result)


def preview(settings: Settings, now: datetime) -> dict[str, Any]:
    with database(settings) as db:
        temp = expired_files(
            settings, now, logs=False, days=setting(db, "housekeeping.temp_days", 7)
        )
        logs = expired_files(
            settings, now, logs=True, days=setting(db, "housekeeping.log_days", 45)
        )
        chunks, vectors = orphan_chunks(db), orphan_vectors(db)
        backups = sorted(
            (settings.data_dir / "backups").glob("pfc-*-housekeeping.db"), reverse=True
        )
        return {
            "temp": {"count": len(temp), "bytes": sum(p.stat().st_size for p in temp)},
            "logs": {"count": len(logs), "bytes": sum(p.stat().st_size for p in logs)},
            "orphan_chunks": len(chunks),
            "orphan_vectors": sum(map(len, vectors.values())),
            "db_bytes": (settings.data_dir / "db" / "pfc.db").stat().st_size,
            "backups": [{"name": p.name, "bytes": p.stat().st_size} for p in backups],
        }


def run(
    settings: Settings, options: dict[str, bool], user_id: int, now: datetime
) -> dict[str, int]:
    result: dict[str, int] = {}
    with database(settings) as db:
        for key, logs, default in (("purge_temp", False, 7), ("purge_logs", True, 45)):
            count = 0
            if options.get(key):
                days = setting(
                    db, "housekeeping.log_days" if logs else "housekeeping.temp_days", default
                )
                for path in expired_files(settings, now, logs=logs, days=days):
                    try:
                        path.unlink()
                        count += 1
                    except (FileNotFoundError, PermissionError):
                        continue
            result[key] = count
        if options.get("purge_orphan_vectors"):
            db.execute("BEGIN IMMEDIATE")
            chunks, vectors = orphan_chunks(db), orphan_vectors(db)
            delete_chunks(db, chunks)
            for table, ids in vectors.items():
                for rowid in ids:
                    db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,))
            result["orphan_chunks"] = len(chunks)
            result["orphan_vectors"] = sum(map(len, vectors.values()))
            db.commit()
        if options.get("vacuum"):
            db.execute("VACUUM")
            result["vacuum"] = 1
        if options.get("backup"):
            directory = settings.data_dir / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"pfc-{now.strftime('%Y%m%dT%H%M%S%fZ')}-housekeeping.db"
            with closing(sqlite3.connect(path)) as target:
                db.backup(target)
            keep = setting(db, "housekeeping.backups_keep", 3)
            for old in sorted(directory.glob("pfc-*-housekeeping.db"), reverse=True)[keep:]:
                old.unlink()
            result["backup"] = 1
        append_audit(
            db, ts=now.isoformat(), user_id=user_id, action="HOUSEKEEPING_RUN", details=result
        )
    return result
