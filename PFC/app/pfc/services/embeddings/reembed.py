"""Owner-atomic, resumable re-indexing (REQ-LLM-07, TDD §6.6)."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import cast

import numpy as np

from pfc.adapters.vector.embeddings import Embedder, Vectors
from pfc.adapters.vector.stores import NumpyStore
from pfc.config import Settings
from pfc.jobs.runner import JobContext
from pfc.services.embeddings.chunking import (
    Chunk,
    Owner,
    OwnerType,
    chunk_owner,
    load_owner,
    load_owners,
    pipeline_version,
)
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable, verify_model_file

_EMBEDDING_LOCK = Lock()
BATCH_SIZE = 32


class EmbeddingSourceChanged(RuntimeError):
    pass


class EmbeddingPipelineMismatch(ValueError):
    pass


@dataclass
class ReembedCounts:
    embedded: int = 0
    reused: int = 0
    skipped: int = 0
    deleted: int = 0


@contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


@contextmanager
def _exclusive(ctx: JobContext) -> Iterator[None]:
    while not _EMBEDDING_LOCK.acquire(timeout=0.1):
        ctx.raise_if_cancelled()
    try:
        ctx.raise_if_cancelled()
        yield
    finally:
        _EMBEDDING_LOCK.release()


def _reusable(
    db: sqlite3.Connection, store: NumpyStore, digest: str, model_id: int, embedder: Embedder
) -> Vectors | None:
    for (id,) in db.execute(
        "SELECT chunk_id FROM embedding_chunks WHERE text_sha256=? AND embedding_model_id=? "
        "ORDER BY chunk_id",
        (digest, model_id),
    ):
        if store.get_model_version(id) == embedder.model_version:
            vector = store.get_vector(id)
            if vector is not None and vector.shape == (embedder.dimensions,):
                return vector
    return None


def _owner(
    db: sqlite3.Connection,
    ctx: JobContext,
    owner: Owner,
    expected: Owner | None,
    model_id: int,
    embedder: Embedder,
    store: NumpyStore,
    done: int,
    total: int,
) -> ReembedCounts:
    chunks = chunk_owner(owner, embedder) if expected is not None else []
    counts = ReembedCounts()
    previous = {
        (row[1], row[2]): (row[0], row[3])
        for row in db.execute(
            "SELECT chunk_id,section,chunk_no,text_sha256 FROM embedding_chunks "
            "WHERE embedding_model_id=? AND owner_type=? AND owner_id=?",
            (model_id, owner.owner_type, owner.owner_id),
        )
    }
    writes: list[tuple[Chunk, str, Vectors]] = []
    missing: dict[str, list[Chunk]] = {}
    cache: dict[str, Vectors] = {}
    for chunk in chunks:
        digest = hashlib.sha256(chunk.model_text.encode()).hexdigest()
        prior = previous.get((chunk.section, chunk.chunk_no))
        if (
            prior
            and prior[1] == digest
            and store.get_model_version(prior[0]) == embedder.model_version
        ):
            vector = store.get_vector(prior[0])
            if vector is not None and vector.shape == (embedder.dimensions,):
                counts.skipped += 1
                cache[digest] = vector
                continue
        vector = cache.get(digest)
        if vector is None:
            vector = _reusable(db, store, digest, model_id, embedder)
        if vector is not None:
            cache[digest] = vector
            writes.append((chunk, digest, vector))
            counts.reused += 1
        else:
            missing.setdefault(digest, []).append(chunk)
    pending = list(missing.items())
    for start in range(0, len(pending), BATCH_SIZE):
        ctx.raise_if_cancelled()
        ctx.report_progress(
            done / total,
            f"Embedding {owner.owner_type} {owner.owner_id}, batch {start // BATCH_SIZE + 1}",
        )
        batch = pending[start : start + BATCH_SIZE]
        vectors = embedder.embed([entries[0].text for _, entries in batch], task=owner.task)
        if vectors.shape != (len(batch), embedder.dimensions) or not np.all(np.isfinite(vectors)):
            raise ValueError("Embedder returned an invalid vector matrix")
        counts.embedded += len(batch)
        for (digest, entries), vector in zip(batch, vectors, strict=True):
            writes.extend((chunk, digest, vector) for chunk in entries)
            counts.reused += len(entries) - 1
        ctx.raise_if_cancelled()
    desired = {(chunk.section, chunk.chunk_no) for chunk in chunks}
    ctx.raise_if_cancelled()
    with _transaction(db):
        if load_owner(db, owner.owner_type, owner.owner_id) != expected:
            raise EmbeddingSourceChanged("Owner changed during embedding; rerun to resume")
        now = datetime.now(UTC).isoformat()
        for chunk, digest, vector in writes:
            row = db.execute(
                "INSERT INTO embedding_chunks(owner_type,owner_id,section,chunk_no,text,"
                "text_sha256,token_count,embedding_model_id,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_type,owner_id,section,chunk_no,embedding_model_id) DO UPDATE "
                "SET text=excluded.text,text_sha256=excluded.text_sha256,"
                "token_count=excluded.token_count,created_at=excluded.created_at "
                "RETURNING chunk_id",
                (
                    chunk.owner_type,
                    chunk.owner_id,
                    chunk.section,
                    chunk.chunk_no,
                    chunk.model_text,
                    digest,
                    chunk.token_count,
                    model_id,
                    now,
                ),
            ).fetchone()
            store.upsert(
                row[0],
                vector,
                embedder.model_version,
                namespace=chunk.owner_type,
                metadata={"embedding_model_id": model_id},
            )
        rows = db.execute(
            "SELECT chunk_id,section,chunk_no FROM embedding_chunks "
            "WHERE embedding_model_id=? AND owner_type=? AND owner_id=?",
            (model_id, owner.owner_type, owner.owner_id),
        ).fetchall()
        for id, section, number in rows:
            if (section, number) not in desired:
                store.delete(id)
                db.execute("DELETE FROM embedding_chunks WHERE chunk_id=?", (id,))
                counts.deleted += 1
        ctx.raise_if_cancelled()
    return counts


def _prepare_model(
    settings: Settings, db: sqlite3.Connection, model_id: int, embedder: Embedder
) -> tuple[int, str, str]:
    path = next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")
    if Path(path).resolve() != (settings.data_dir / "db" / "pfc.db").resolve():
        raise ValueError("Vector store must use the application database")
    if db.in_transaction:
        raise ValueError("Re-embedding requires a connection without an open transaction")
    with _transaction(db):
        row = db.execute(
            "SELECT dim,file_sha256,pipeline_version FROM embedding_models "
            "WHERE embedding_model_id=?",
            (model_id,),
        ).fetchone()
        if row is None:
            raise EmbeddingModelUnavailable(f"Embedding model {model_id} is not registered")
        if embedder.dimensions != row[0]:
            raise ValueError("Embedder dimensions do not match the registered embedding model")
        if row[1]:
            model_path = getattr(embedder, "model_path", None)
            if model_path is None:
                raise EmbeddingModelUnavailable(
                    "Cannot verify the registered model without a model file"
                )
            verify_model_file(Path(model_path), row[1])
        version = pipeline_version(row[2])
        if version != row[2]:
            if (
                row[2]
                and db.execute(
                    "SELECT 1 FROM embedding_chunks WHERE embedding_model_id=? LIMIT 1", (model_id,)
                ).fetchone()
            ):
                raise EmbeddingPipelineMismatch(
                    "Pipeline differs on a populated model; run a full re-embed "
                    "using a new embedding model row for the new chunking pipeline"
                )
            db.execute(
                "UPDATE embedding_models SET pipeline_version=? WHERE embedding_model_id=?",
                (version, model_id),
            )
    return int(row[0]), str(row[1]), version


def embed_owners(
    settings: Settings,
    ctx: JobContext,
    *,
    owners: Iterable[tuple[OwnerType, int]],
    embedder: Embedder,
    store: NumpyStore,
    embedding_model_id: int,
) -> ReembedCounts:
    """Index only requested owners, cleaning retired owners without activating a model."""
    selected = sorted(set(owners))
    if any(kind not in ("cv", "jd", "jd_item") for kind, _ in selected):
        raise ValueError("Unsupported embedding owner type")
    with _exclusive(ctx):
        db = store.connection
        _prepare_model(settings, db, embedding_model_id, embedder)
        counts = ReembedCounts()
        for done, (kind, id) in enumerate(selected):
            ctx.raise_if_cancelled()
            expected = load_owner(db, kind, id)
            owner = expected if expected is not None else Owner(kind, id, "")
            result = _owner(
                db, ctx, owner, expected, embedding_model_id, embedder, store, done, len(selected)
            )
            for field in ("embedded", "reused", "skipped", "deleted"):
                setattr(counts, field, getattr(counts, field) + getattr(result, field))
            ctx.report_progress((done + 1) / len(selected), f"Indexed {kind} {id}")
        ctx.raise_if_cancelled()
        return counts


def reembed(
    settings: Settings,
    ctx: JobContext,
    *,
    embedding_model_id: int,
    embedder: Embedder,
    store: NumpyStore,
) -> ReembedCounts:
    """Commit each owner as a checkpoint; activate only after a complete stable scan.

    Deliberately retain other models' rows/vectors after the atomic activation flip.
    The store must use the application's database and have no open transaction.
    Inference/progress occur outside write transactions so cancellation stays live.
    A new empty model row acquires the chunker pipeline version; changing a populated
    row's pipeline requires a new model row rather than relabelling old vectors.
    """
    db = store.connection
    with _exclusive(ctx):
        identity = _prepare_model(settings, db, embedding_model_id, embedder)
        source = load_owners(db)
        owners = {(owner.owner_type, owner.owner_id): owner for owner in source}
        for kind, id in db.execute(
            "SELECT DISTINCT owner_type,owner_id FROM embedding_chunks WHERE embedding_model_id=? "
            "AND owner_type IN ('cv','jd','jd_item')",
            (embedding_model_id,),
        ):
            owners.setdefault((kind, id), Owner(cast(OwnerType, kind), id, ""))
        current = {(owner.owner_type, owner.owner_id): owner for owner in source}
        total, counts = len(owners), ReembedCounts()
        ctx.report_progress(0 if total else 1, "Preparing embedding owners")
        for done, (key, owner) in enumerate(sorted(owners.items())):
            ctx.raise_if_cancelled()
            result = _owner(
                db, ctx, owner, current.get(key), embedding_model_id, embedder, store, done, total
            )
            for field in ("embedded", "reused", "skipped", "deleted"):
                setattr(counts, field, getattr(counts, field) + getattr(result, field))
            ctx.report_progress((done + 1) / total, f"Indexed {owner.owner_type} {owner.owner_id}")
        ctx.raise_if_cancelled()
        with _transaction(db):
            if load_owners(db) != source:
                raise EmbeddingSourceChanged(
                    "Owners changed during indexing; rerun before activation"
                )
            registered = db.execute(
                "SELECT dim,file_sha256,pipeline_version FROM embedding_models "
                "WHERE embedding_model_id=?",
                (embedding_model_id,),
            ).fetchone()
            if registered is None or tuple(registered) != identity:
                raise EmbeddingSourceChanged("Registered embedding model changed during indexing")
            ctx.raise_if_cancelled()
            db.execute(
                "UPDATE embedding_models SET is_active=0 "
                "WHERE is_active=1 AND embedding_model_id<>?",
                (embedding_model_id,),
            )
            db.execute(
                "UPDATE embedding_models SET is_active=1 WHERE embedding_model_id=?",
                (embedding_model_id,),
            )
        return counts
