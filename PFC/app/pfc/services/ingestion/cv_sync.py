"""REQ-IN-20..25: cancellable local CV persistence and optional offline embedding."""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pfc.adapters.sources.resume_folder import CandidateLookup, CVVersion, ResumeFolderSource
from pfc.adapters.sources.resume_paths import read_resume, resume_files
from pfc.adapters.vector.embeddings import Embedder
from pfc.adapters.vector.stores import create_vector_store
from pfc.api.schemas.cv import CVSyncCounts, CVVersionItem
from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.domain.normalise import normalise_term
from pfc.domain.normalise.text import normalise_text
from pfc.jobs.runner import JobContext
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services.embeddings.chunking import OwnerType
from pfc.services.embeddings.reembed import ReembedCounts, embed_owners
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable, create_embedder
from pfc.services.occ.synonyms import valid_synonym_table

from .cv_facts import (
    FACTS_SCHEMA_VERSION,
    ExtractedFacts,
    LLMBridge,
    extract_facts,
    public_facts,
    validate_facts,
)
from .limits import positive_setting
from .masking import PHONE

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
            "v.identity_match,v.is_latest,v.extracted_at,v.facts_json FROM cv_versions v "
            "JOIN resumes r ON r.resume_id=v.resume_id WHERE v.candidate_id=? "
            "ORDER BY v.version_no DESC",
            (candidate_id,),
        ).fetchall()
        return [CVVersionItem(**dict(r), facts=public_facts(r["facts_json"])) for r in rows]


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


class _EmbeddingContext(JobContext):
    def __init__(self, parent: JobContext, index: int, total: int) -> None:
        super().__init__(parent.runner, parent.job_id, parent.cancelled)
        self.parent, self.index, self.total = parent, index, total

    def report_progress(self, fraction: float, message: str = "") -> None:
        self.parent.report_progress((self.index + fraction) / self.total, message)


def _embed_affected(
    settings: Settings,
    context: JobContext,
    owners: list[tuple[OwnerType, int]],
    embedder: Embedder | None,
) -> tuple[ReembedCounts, str | None]:
    with database(settings) as db:
        model = db.execute(
            "SELECT embedding_model_id FROM embedding_models WHERE is_active=1"
        ).fetchone()
        if model is None:
            return ReembedCounts(), "Embedding skipped: no active embedding model"
        try:
            selected = (
                embedder
                if embedder is not None
                else create_embedder(settings, embedding_model_id=model[0])
            )
            store = create_vector_store(db, dimensions=selected.dimensions)
            return embed_owners(
                settings,
                context,
                owners=owners,
                embedder=selected,
                store=store,
                embedding_model_id=model[0],
            ), None
        except EmbeddingModelUnavailable as exc:
            context.raise_if_cancelled()
            return ReembedCounts(), f"Embedding skipped: {exc}"


def _replace_skills(db: sqlite3.Connection, cv_version_id: int) -> None:
    version = db.execute(
        "SELECT candidate_id,text_norm,facts_json,extraction_removed_at FROM cv_versions "
        "WHERE cv_version_id=?",
        (cv_version_id,),
    ).fetchone()
    db.execute(
        "DELETE FROM resource_skills WHERE source='CV' AND cv_version_id=?", (cv_version_id,)
    )
    if version["extraction_removed_at"] is not None:
        return
    facts = validate_facts(
        version["text_norm"], json.loads(version["facts_json"] or "{}").get("facts", {})
    )
    resources = db.execute(
        "SELECT resource_id FROM resources WHERE candidate_id=?", (version["candidate_id"],)
    ).fetchall()
    for skill in facts.skills:
        if "@" in skill.value or PHONE.search(skill.value):
            continue
        canonical = db.execute(
            "SELECT skill_id FROM skills WHERE name=? COLLATE NOCASE AND is_active=1",
            (skill.canonical,),
        ).fetchone()
        db.executemany(
            "INSERT INTO resource_skills(resource_id,cv_version_id,source,skill_raw,skill_norm,"
            "canonical_skill_id,evidence,years) VALUES (?,?,'CV',?,?,?,?,NULL)",
            [
                (
                    resource[0],
                    cv_version_id,
                    skill.value,
                    normalise_term(skill.value),
                    canonical[0] if canonical else None,
                    skill.evidence,
                )
                for resource in resources
            ],
        )


def _persist(
    db: sqlite3.Connection,
    candidate_id: int | None,
    version: CVVersion,
    now: str,
    root: Path,
    extracted: ExtractedFacts | None,
) -> tuple[int, bool]:
    path = Path(version.source_path)
    db.execute(
        "INSERT INTO resumes(candidate_id,source_path,file_name,file_sha256,file_mtime,mime_type,"
        "discovered_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(file_sha256) DO NOTHING",
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
    resume = db.execute(
        "SELECT resume_id,source_path FROM resumes WHERE file_sha256=?", (version.file_sha256,)
    ).fetchone()
    resume_id = resume["resume_id"]
    try:
        content, _ = read_resume(root, resume["source_path"], version.file_size)
        original_matches = (
            content is not None and hashlib.sha256(content).hexdigest() == version.file_sha256
        )
    except OSError:
        original_matches = False
    if not original_matches:
        db.execute(
            "UPDATE resumes SET source_path=?,file_name=?,file_mtime=? WHERE resume_id=?",
            (
                version.source_path,
                path.name,
                stamp(datetime.fromtimestamp(version.file_mtime, UTC)),
                resume_id,
            ),
        )
    text = normalise_text(version.text_norm)
    digest = hashlib.sha256(text.encode()).hexdigest()
    existing = db.execute(
        "SELECT cv_version_id,candidate_id,text_norm,facts_json,extraction_removed_at "
        "FROM cv_versions "
        "WHERE resume_id=? AND text_sha256=?",
        (resume_id, digest),
    ).fetchone()
    if existing:
        if existing["extraction_removed_at"] is not None and extracted is not None:
            db.execute(
                "UPDATE cv_versions SET text_norm=?,sections_json=NULL,facts_json=?,"
                "extracted_by=?,prompt_version=?,extraction_removed_at=NULL,extracted_at=? "
                "WHERE cv_version_id=?",
                (
                    text,
                    json.dumps(
                        {
                            "emails": EMAIL.findall(version.text_norm),
                            "facts_schema": FACTS_SCHEMA_VERSION,
                            "facts": validate_facts(
                                text, extracted.facts.model_dump()
                            ).model_dump(),
                        }
                    ),
                    extracted.extracted_by,
                    extracted.prompt_version,
                    now,
                    existing["cv_version_id"],
                ),
            )
        elif existing["extraction_removed_at"] is None:
            saved = json.loads(existing["facts_json"] or "{}")
            if saved.get("facts_schema", 0) < FACTS_SCHEMA_VERSION and extracted is not None:
                saved.update(
                    facts=validate_facts(
                        existing["text_norm"], extracted.facts.model_dump()
                    ).model_dump(),
                    facts_schema=FACTS_SCHEMA_VERSION,
                )
                db.execute(
                    "UPDATE cv_versions SET facts_json=?,extracted_by=?,prompt_version=? "
                    "WHERE cv_version_id=?",
                    (
                        json.dumps(saved),
                        extracted.extracted_by,
                        extracted.prompt_version,
                        existing["cv_version_id"],
                    ),
                )
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
        _replace_skills(db, existing[0])
        return existing[0], False
    number = db.execute(
        "SELECT coalesce(max(version_no),0)+1 FROM cv_versions WHERE candidate_id IS ?",
        (candidate_id,),
    ).fetchone()[0]
    assert extracted is not None
    cursor = db.execute(
        "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
        "facts_json,extracted_by,prompt_version,identity_match,is_latest,extracted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?)",
        (
            resume_id,
            candidate_id,
            number,
            digest,
            text,
            json.dumps(
                {
                    "emails": EMAIL.findall(version.text_norm),
                    "facts_schema": FACTS_SCHEMA_VERSION,
                    "facts": validate_facts(text, extracted.facts.model_dump()).model_dump(),
                }
            ),
            extracted.extracted_by,
            extracted.prompt_version,
            version.identity_match,
            now,
        ),
    )
    assert cursor.lastrowid is not None
    _replace_skills(db, cursor.lastrowid)
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
    facts_bridge: LLMBridge | None = None,
) -> None:
    context.raise_if_cancelled()
    lookups = _lookups(settings, ids)
    inventory = set()
    if ids:
        for path in resume_files(root):
            context.raise_if_cancelled()
            inventory.add(path.relative_to(root.resolve()).as_posix())
    counts = CVSyncCounts(scanned=len(inventory))
    with database(settings) as db:
        facts_timeout = positive_setting(db, "cv.llm_timeout_s", 180)
        skill_synonyms = valid_synonym_table(db)
        source = ResumeFolderSource(
            root,
            work_dir=settings.data_dir / "work" / "cv-extraction",
            max_file_bytes=max(1, int(positive_setting(db, "cv.max_file_mb", 20) * 1024**2)),
            extract_timeout_s=positive_setting(db, "cv.extract_timeout_s", 30),
        )
    embedding_counts = ReembedCounts()
    embedding_notes: list[str] = []
    matched: dict[str, dict[str, CVVersion]] = defaultdict(dict)
    for result in source.scan(lookups):
        context.raise_if_cancelled()
        for version in result.versions:
            matched[version.source_path][result.candidate_id] = version
    counts.missing_cv = len(
        {c.candidate_id for c in lookups} - {c for matches in matched.values() for c in matches}
    )
    unmatched = inventory - matched.keys()
    if unmatched:
        for result in source.scan(
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
            if identity != "unmatched" and not {"too_large", "extraction_timeout"}.intersection(
                version.flags
            ):
                identities[version.file_sha256].add(identity)
    for matches in matched.values():
        version = next(iter(matches.values()))
        if "too_large" in version.flags:
            counts.too_large += 1
            continue
        if "extraction_timeout" in version.flags:
            counts.extraction_timeout += 1
            continue
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
    facts_cache: dict[str, ExtractedFacts] = {}
    for index, (candidate_id, found) in enumerate(groups.items()):
        context.raise_if_cancelled()
        prepared: dict[str, ExtractedFacts] = {}
        for version in found:
            text = normalise_text(version.text_norm)
            text_digest = hashlib.sha256(text.encode()).hexdigest()
            with database(settings) as db:
                previous = db.execute(
                    "SELECT v.facts_json,v.extraction_removed_at FROM cv_versions v "
                    "JOIN resumes r USING(resume_id) "
                    "WHERE r.file_sha256=? AND v.text_sha256=?",
                    (version.file_sha256, text_digest),
                ).fetchone()
            if (
                previous is None
                or previous["extraction_removed_at"] is not None
                or json.loads(previous["facts_json"] or "{}").get("facts_schema", 0)
                < FACTS_SCHEMA_VERSION
            ):
                if text_digest not in facts_cache:
                    facts_cache[text_digest] = extract_facts(
                        text, facts_bridge, facts_timeout, context
                    )
                    for skill in facts_cache[text_digest].facts.skills:
                        skill.canonical = skill_synonyms.resolve(skill.value)
                extracted = facts_cache[text_digest]
                prepared[version.source_path] = extracted
                counts.facts_llm_timeout += int(extracted.failure == "timeout")
                counts.facts_llm_failed += int(extracted.failure == "error")
        with database(settings) as db:
            db.execute("BEGIN IMMEDIATE")
            affected = (
                {
                    row[0]
                    for row in db.execute(
                        "SELECT v.cv_version_id FROM cv_versions v WHERE v.candidate_id=? AND "
                        "(v.is_latest=1 OR EXISTS (SELECT 1 FROM embedding_chunks c "
                        "JOIN embedding_models m USING(embedding_model_id) WHERE m.is_active=1 "
                        "AND c.owner_type='cv' AND c.owner_id=v.cv_version_id))",
                        (candidate_id,),
                    )
                }
                if candidate_id is not None
                else set()
            )
            now = stamp(datetime.now(UTC))
            for version in sorted(found, key=lambda v: (v.file_mtime, v.source_path)):
                context.raise_if_cancelled()
                _, inserted = _persist(
                    db, candidate_id, version, now, root, prepared.get(version.source_path)
                )
                counts.new_versions += int(inserted)
                counts.unchanged += int(not inserted)
            if candidate_id is not None:
                _latest(db, candidate_id)
                affected.update(
                    row[0]
                    for row in db.execute(
                        "SELECT cv_version_id FROM cv_versions "
                        "WHERE candidate_id=? AND is_latest=1",
                        (candidate_id,),
                    )
                )
            context.raise_if_cancelled()
        if affected:
            embedded, note = _embed_affected(
                settings,
                _EmbeddingContext(context, index, len(groups)),
                [("cv", id) for id in sorted(affected)],
                embedder,
            )
            for field in ("embedded", "reused", "skipped", "deleted"):
                setattr(
                    embedding_counts,
                    field,
                    getattr(embedding_counts, field) + getattr(embedded, field),
                )
            counts.embedded += embedded.embedded
            if note and note not in embedding_notes:
                embedding_notes.append(note)
        context.report_progress((index + 1) / max(len(groups), 1), "Synchronising CVs")
    context.raise_if_cancelled()
    summary = {
        **counts.model_dump(),
        "embedding_counts": asdict(embedding_counts),
        "embedding_notes": embedding_notes,
    }
    with database(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        append_audit(
            db,
            ts=stamp(datetime.now(UTC)),
            action="CV_SYNC",
            user_id=user_id,
            entity_type="job",
            entity_id=str(context.job_id),
            details=summary,
        )
        # TODO(foundation): use JobContext.complete(message) in pfc/jobs/runner.py.
        db.execute(
            "UPDATE jobs SET status='completed',progress=1,message=?,finished_at=? WHERE job_id=?",
            (json.dumps(summary), stamp(datetime.now(UTC)), context.job_id),
        )
