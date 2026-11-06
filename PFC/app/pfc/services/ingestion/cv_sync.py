"""REQ-IN-20..25: cancellable local CV persistence and optional offline embedding."""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pfc.adapters.sources.resume_folder import CandidateLookup, CVVersion, ResumeFolderSource
from pfc.adapters.vector.embeddings import Embedder
from pfc.adapters.vector.stores import create_vector_store
from pfc.api.schemas.cv import CVSyncCounts, CVVersionItem
from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.domain.normalise.text import normalise_text
from pfc.jobs.runner import JobContext
from pfc.security.problems import Problem
from pfc.security.sessions import stamp

EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")
MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def prepare(
    settings: Settings, user_id: int, resource_ids: list[int] | None
) -> tuple[Path, list[int]]:
    with database(settings) as db:
        row = db.execute(
            "SELECT value_json FROM settings WHERE key='resume.root_path' AND "
            "((scope='user' AND user_id=?) OR scope='system') "
            "ORDER BY CASE scope WHEN 'user' THEN 0 ELSE 1 END LIMIT 1",
            (user_id,),
        ).fetchone()
        value = json.loads(row[0]) if row else None
        if not isinstance(value, str) or not value.strip() or not Path(value).is_dir():
            raise Problem(
                409, "RESUME_PATH_NOT_CONFIGURED", "Configure an existing resume directory"
            )
        if resource_ids is None:
            ids = [
                r[0]
                for r in db.execute("SELECT resource_id FROM resources WHERE lifecycle='active'")
            ]
        else:
            ids = sorted(set(resource_ids))
            for resource_id in ids:
                if not db.execute(
                    "SELECT 1 FROM resources WHERE resource_id=?", (resource_id,)
                ).fetchone():
                    raise Problem(404, "RESOURCE_NOT_FOUND", "Resource not found")
    return Path(value), ids


def versions(settings: Settings, candidate_id: int) -> list[CVVersionItem]:
    with database(settings) as db:
        if not db.execute(
            "SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone():
            raise Problem(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
        rows = db.execute(
            "SELECT v.cv_version_id,v.version_no,r.file_name,r.source_path,r.file_mtime,"
            "v.identity_match,v.is_latest,v.extracted_at FROM cv_versions v "
            "JOIN resumes r ON r.resume_id=v.resume_id WHERE v.candidate_id=? "
            "ORDER BY v.version_no DESC",
            (candidate_id,),
        ).fetchall()
        return [CVVersionItem(**dict(r)) for r in rows]


def _lookups(settings: Settings, ids: list[int]) -> list[CandidateLookup]:
    result = []
    with database(settings) as db:
        for resource_id in ids:
            row = db.execute(
                "SELECT r.*,c.display_name FROM resources r JOIN candidates c "
                "ON c.candidate_id=r.candidate_id WHERE r.resource_id=?",
                (resource_id,),
            ).fetchone()
            if row is None:
                continue
            emails = [
                r[0]
                for r in db.execute(
                    "SELECT email FROM candidate_emails WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
            ] or [row["email_address"]]
            paths = [
                r[0]
                for r in db.execute(
                    "SELECT source_path FROM resumes WHERE candidate_id=?", (row["candidate_id"],)
                )
            ]
            for email in emails:
                result.append(
                    CandidateLookup(
                        candidate_id=str(row["candidate_id"]),
                        short_name=row["short_name"],
                        display_name=row["display_name"] or "",
                        email_address=email,
                        source_paths=paths,
                    )
                )
    return result


def _embed(
    db: sqlite3.Connection,
    version_id: int,
    text: str,
    embedder: Embedder | None,
    now: str,
    context: JobContext,
) -> int:
    model = db.execute("SELECT * FROM embedding_models WHERE is_active=1").fetchone()
    if embedder is None or model is None or not text:
        return 0
    if model["dim"] != embedder.dimensions:
        raise ValueError("Active embedding model dimensions do not match embedder")
    words = text.split()
    chunks = [" ".join(words[i : i + 200]) for i in range(0, len(words), 200)]
    pending = [
        (number, chunk)
        for number, chunk in enumerate(chunks)
        if not db.execute(
            "SELECT 1 FROM embedding_chunks WHERE owner_type='cv' AND owner_id=? "
            "AND section='full' AND chunk_no=? AND embedding_model_id=?",
            (version_id, number, model["embedding_model_id"]),
        ).fetchone()
    ]
    if not pending:
        return 0
    context.raise_if_cancelled()
    vectors = embedder.embed([chunk for _, chunk in pending], task="document")
    if vectors.shape != (len(pending), embedder.dimensions):
        raise ValueError("Embedder returned an unexpected vector shape")
    store = create_vector_store(db, dimensions=embedder.dimensions)
    for (number, chunk), vector in zip(pending, vectors, strict=True):
        context.raise_if_cancelled()
        cursor = db.execute(
            "INSERT INTO embedding_chunks(owner_type,owner_id,section,chunk_no,text,text_sha256,"
            "token_count,embedding_model_id,created_at) VALUES ('cv',?,'full',?,?,?,?,?,?)",
            (
                version_id,
                number,
                chunk,
                hashlib.sha256(chunk.encode()).hexdigest(),
                len(chunk.split()),
                model["embedding_model_id"],
                now,
            ),
        )
        assert cursor.lastrowid is not None
        store.upsert(
            cursor.lastrowid,
            vector,
            embedder.model_version,
            namespace="cv",
            metadata={"embedding_model_id": model["embedding_model_id"]},
        )
    return len(pending)


def _persist(
    db: sqlite3.Connection,
    candidate_id: int | None,
    version: CVVersion,
    now: str,
) -> tuple[int, bool]:
    path = Path(version.source_path)
    db.execute(
        "INSERT INTO resumes(candidate_id,source_path,file_name,file_sha256,file_mtime,mime_type,"
        "discovered_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(file_sha256) DO UPDATE SET "
        "source_path=excluded.source_path,file_name=excluded.file_name,"
        "file_mtime=excluded.file_mtime,mime_type=excluded.mime_type",
        (
            candidate_id,
            version.source_path,
            path.name,
            version.file_sha256,
            stamp(datetime.fromtimestamp(version.file_mtime, UTC)),
            MIMES[path.suffix.lower()],
            now,
        ),
    )
    resume_id = db.execute(
        "SELECT resume_id FROM resumes WHERE file_sha256=?", (version.file_sha256,)
    ).fetchone()[0]
    text = normalise_text(version.text_norm)
    digest = hashlib.sha256(text.encode()).hexdigest()
    existing = db.execute(
        "SELECT cv_version_id,candidate_id FROM cv_versions WHERE resume_id=? AND text_sha256=?",
        (resume_id, digest),
    ).fetchone()
    if existing:
        if existing["candidate_id"] is None and candidate_id is not None:
            number = db.execute(
                "SELECT coalesce(max(version_no),0)+1 FROM cv_versions WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()[0]
            db.execute(
                "UPDATE cv_versions SET candidate_id=?,version_no=?,identity_match=? "
                "WHERE cv_version_id=?",
                (candidate_id, number, version.identity_match, existing["cv_version_id"]),
            )
            db.execute(
                "UPDATE resumes SET candidate_id=? WHERE resume_id=? AND candidate_id IS NULL",
                (candidate_id, resume_id),
            )
        return existing[0], False
    number = db.execute(
        "SELECT coalesce(max(version_no),0)+1 FROM cv_versions WHERE candidate_id IS ?",
        (candidate_id,),
    ).fetchone()[0]
    cursor = db.execute(
        "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
        "facts_json,extracted_by,identity_match,is_latest,extracted_at) "
        "VALUES (?,?,?,?,?,?,'rule_based',?,0,?)",
        (
            resume_id,
            candidate_id,
            number,
            digest,
            text,
            json.dumps({"emails": EMAIL.findall(version.text_norm)}),
            version.identity_match,
            now,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid, True


def _latest(db: sqlite3.Connection, candidate_id: int) -> None:
    rows = db.execute(
        "SELECT v.cv_version_id FROM cv_versions v JOIN resumes r ON r.resume_id=v.resume_id "
        "WHERE v.candidate_id=? ORDER BY r.file_mtime, r.source_path, v.cv_version_id",
        (candidate_id,),
    ).fetchall()
    db.execute(
        "UPDATE cv_versions SET version_no=-cv_version_id,is_latest=0 WHERE candidate_id=?",
        (candidate_id,),
    )
    for number, row in enumerate(rows, 1):
        db.execute(
            "UPDATE cv_versions SET version_no=?,is_latest=? WHERE cv_version_id=?",
            (number, number == len(rows), row[0]),
        )


def sync(
    context: JobContext,
    settings: Settings,
    root: Path,
    ids: list[int],
    user_id: int,
    embedder: Embedder | None = None,
) -> None:
    context.raise_if_cancelled()
    lookups = _lookups(settings, ids)
    inventory = set()
    if ids:
        for path in root.rglob("*"):
            context.raise_if_cancelled()
            if (
                path.is_file()
                and not path.name.startswith("~$")
                and path.suffix.lower() in {".pdf", ".docx", ".txt"}
            ):
                inventory.add(path.relative_to(root).as_posix())
    counts = CVSyncCounts(scanned=len(inventory))
    matched: dict[str, dict[str, CVVersion]] = defaultdict(dict)
    for result in ResumeFolderSource(root).scan(lookups):
        context.raise_if_cancelled()
        for version in result.versions:
            matched[version.source_path][result.candidate_id] = version
    counts.missing_cv = len(
        {c.candidate_id for c in lookups} - {c for matches in matched.values() for c in matches}
    )
    unmatched = inventory - matched.keys()
    if unmatched:
        for result in ResumeFolderSource(root).scan(
            [CandidateLookup(candidate_id="unmatched", source_paths=sorted(unmatched))]
        ):
            for version in result.versions:
                matched[version.source_path] = {
                    "unmatched": version.model_copy(update={"identity_match": "none"})
                }
    groups: dict[int | None, list[CVVersion]] = defaultdict(list)
    identities: dict[str, set[str]] = defaultdict(set)
    for matches in matched.values():
        for identity, version in matches.items():
            if identity != "unmatched":
                identities[version.file_sha256].add(identity)
    for matches in matched.values():
        version = next(iter(matches.values()))
        if any(
            "extraction failed" in flag or flag == "no extractable text" for flag in version.flags
        ):
            counts.extraction_failed += 1
        if len(matches) > 1 or len(identities[version.file_sha256]) > 1:
            counts.ambiguous += 1
            candidate_id = None
            version = version.model_copy(update={"identity_match": "ambiguous"})
        elif "unmatched" in matches:
            counts.unmatched += 1
            candidate_id = None
        else:
            candidate_id = int(next(iter(matches)))
            if version.identity_match == "ambiguous":
                # Multiple lookup aliases for the same candidate are not ambiguous identities.
                email_match = any(
                    c.candidate_id == str(candidate_id)
                    and c.email_address
                    and re.search(
                        r"(?<![a-z0-9._+-])"
                        + re.escape(c.email_address.strip().lower())
                        + r"(?![a-z0-9-]|\.[a-z0-9])",
                        f"{Path(version.source_path).stem}\n{version.text_norm}".lower(),
                    )
                    for c in lookups
                )
                version = version.model_copy(
                    update={"identity_match": "email_exact" if email_match else "name_fuzzy"}
                )
        if Path(version.source_path).suffix.lower() not in MIMES:
            counts.warnings += 1
            continue
        groups[candidate_id].append(version)
    for index, (candidate_id, found) in enumerate(groups.items()):
        context.raise_if_cancelled()
        with database(settings) as db:
            db.execute("BEGIN IMMEDIATE")
            now = stamp(datetime.now(UTC))
            for version in sorted(found, key=lambda v: (v.file_mtime, v.source_path)):
                context.raise_if_cancelled()
                version_id, inserted = _persist(db, candidate_id, version, now)
                counts.new_versions += int(inserted)
                counts.unchanged += int(not inserted)
                if candidate_id is not None:
                    counts.embedded += _embed(
                        db, version_id, normalise_text(version.text_norm), embedder, now, context
                    )
            if candidate_id is not None:
                _latest(db, candidate_id)
            context.raise_if_cancelled()
        context.report_progress((index + 1) / max(len(groups), 1), "Synchronising CVs")
    context.raise_if_cancelled()
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        append_audit(
            db,
            ts=stamp(datetime.now(UTC)),
            action="CV_SYNC",
            user_id=user_id,
            entity_type="job",
            entity_id=str(context.job_id),
            details=counts.model_dump(),
        )
        # The runner clears messages on completion, so publish the terminal summary atomically here.
        db.execute(
            "UPDATE jobs SET status='completed',progress=1,message=?,finished_at=? WHERE job_id=?",
            (counts.model_dump_json(), stamp(datetime.now(UTC)), context.job_id),
        )
