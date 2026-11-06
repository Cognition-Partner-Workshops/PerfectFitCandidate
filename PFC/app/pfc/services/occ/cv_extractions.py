"""REQ-MAT-07: clear extraction payloads without deleting snapshot identities."""

import json
import sqlite3

from pfc.adapters.vector.stores import create_vector_store
from pfc.api.schemas.occ_approvals import CamelModel
from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.services.ingestion.identity import _masked_text


class ExtractionItem(CamelModel):
    cv_version_id: int
    file_name: str
    version_no: int
    extracted_at: str
    removed: bool


class RemovalCounts(CamelModel):
    versions_cleared: int = 0
    chunks_deleted: int = 0
    vectors_deleted: int = 0
    matching_vectors_deleted: int = 0


def _check_candidate(db: sqlite3.Connection, candidate_id: int | None) -> None:
    if (
        candidate_id is not None
        and db.execute("SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        is None
    ):
        raise Problem(404, "CANDIDATE_NOT_FOUND", "Candidate not found")


def list_extractions(db: sqlite3.Connection, candidate_id: int) -> list[ExtractionItem]:
    _check_candidate(db, candidate_id)
    return [
        ExtractionItem(
            cv_version_id=row["cv_version_id"],
            file_name=_masked_text(row["file_name"]),
            version_no=row["version_no"],
            extracted_at=row["extracted_at"],
            removed=row["extraction_removed_at"] is not None,
        )
        for row in db.execute(
            "SELECT v.*,r.file_name FROM cv_versions v JOIN resumes r USING(resume_id) "
            "WHERE v.candidate_id=? ORDER BY v.version_no,v.cv_version_id",
            (candidate_id,),
        )
    ]


def _matching_cache(
    db: sqlite3.Connection,
    settings: Settings,
    version_ids: set[int],
) -> int:
    path = settings.data_dir / "db" / "matching_vectors.db"
    if not path.is_file():
        return 0
    db.execute("ATTACH DATABASE ? AS cv_matching_cache", (str(path),))
    if (
        db.execute(
            "SELECT 1 FROM cv_matching_cache.sqlite_master "
            "WHERE type='table' AND name='pfc_vector_records'"
        ).fetchone()
        is None
    ):
        return 0
    ids = []
    for row in db.execute(
        "SELECT chunk_id,metadata_json FROM cv_matching_cache.pfc_vector_records "
        "WHERE namespace='matching'"
    ).fetchall():
        owner = json.loads(json.loads(row["metadata_json"])["cache_key"])
        if owner["owner_type"] == "cv" and owner["owner_id"] in version_ids:
            ids.append((row["chunk_id"],))
    db.executemany(
        "DELETE FROM cv_matching_cache.pfc_vector_records WHERE chunk_id=?",
        ids,
    )
    return len(ids)


def remove(
    db: sqlite3.Connection,
    settings: Settings,
    user_id: int,
    now: str,
    candidate_id: int | None = None,
) -> RemovalCounts:
    _check_candidate(db, candidate_id)
    rows = db.execute(
        "SELECT cv_version_id,extraction_removed_at FROM cv_versions "
        "WHERE (? IS NULL OR candidate_id=?)",
        (candidate_id, candidate_id),
    ).fetchall()
    version_ids = {row["cv_version_id"] for row in rows}
    counts = RemovalCounts()
    chunks = db.execute(
        "SELECT c.chunk_id FROM embedding_chunks c "
        "JOIN cv_versions v ON c.owner_id=v.cv_version_id "
        "WHERE c.owner_type='cv' AND (? IS NULL OR v.candidate_id=?)",
        (candidate_id, candidate_id),
    ).fetchall()
    if chunks:
        store = create_vector_store(db)
        for row in chunks:
            chunk_id = row["chunk_id"]
            counts.vectors_deleted += int(store.get_model_version(chunk_id) is not None)
            store.delete(chunk_id)
            db.execute("DELETE FROM embedding_chunks WHERE chunk_id=?", (chunk_id,))
            counts.chunks_deleted += 1
    counts.matching_vectors_deleted = _matching_cache(db, settings, version_ids)
    counts.versions_cleared = sum(row["extraction_removed_at"] is None for row in rows)
    db.execute(
        "UPDATE cv_versions SET text_norm='',sections_json=NULL,facts_json='{}',"
        "extraction_removed_at=? WHERE extraction_removed_at IS NULL "
        "AND (? IS NULL OR candidate_id=?)",
        (now, candidate_id, candidate_id),
    )
    append_audit(
        db,
        ts=now,
        user_id=user_id,
        action="CV_EXTRACTIONS_REMOVED",
        entity_type="cv_versions",
        entity_id=str(candidate_id) if candidate_id is not None else None,
        details={
            "scope": "candidate" if candidate_id is not None else "all",
            "candidate_id": candidate_id,
            **counts.model_dump(),
        },
    )
    return counts
