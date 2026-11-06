"""Active ONNX ports and an isolated persistent cache for matching's existing inputs."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Literal

import numpy as np

from pfc.adapters.vector.embeddings import OnnxEmbedder, model_fingerprint
from pfc.adapters.vector.stores import NumpyStore
from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.embeddings.runtime import (
    EmbeddingModelUnavailable,
    create_embedder,
    resolve_model_files,
    verify_model_file,
)
from pfc.services.matching.ports import ChunkOwner, LlmProvider, MatchingPorts, Task
from pfc.services.matching.semantic import model_identity

ResolutionKey = tuple[int, str, str, str, int, str, str]


class MatchingEmbedder:
    runtime: Literal["onnx", "llamacpp"] = "onnx"

    def __init__(self, row: sqlite3.Row, adapter: OnnxEmbedder, key: ResolutionKey) -> None:
        self.embedding_model_id = int(row["embedding_model_id"])
        self.name = str(row["name"])
        self.file_sha256 = str(row["file_sha256"])
        self.dim = int(row["dim"])
        self.pipeline_version = str(row["pipeline_version"])
        self.model_version = adapter.model_version
        self.adapter = adapter
        self.resolution_key = key

    def embed(self, texts: list[str], *, task: Task = "document") -> list[list[float]]:
        return self.adapter.embed(texts, task=task).tolist()


def _cache_key(owner: ChunkOwner) -> str:
    return json.dumps(asdict(owner), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cache_id(key: str) -> int:
    return 1 + int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % ((1 << 63) - 1)


class MatchingVectorStore:
    """Point-cache through NumpyStore; no top-k or semantic aggregation changes.

    IDs are SHA-256(canonical JSON of every ChunkOwner field), first 64 bits,
    reduced modulo (2**63 - 1), plus one. Metadata checks reject hash collisions.
    The adapter's chunk_id PK is global, not namespace-scoped, so matching uses
    db/matching_vectors.db, never the application's embedding_chunks ID space.
    Every operation owns its connection; namespace is always 'matching'.
    """

    def __init__(self, settings: Settings, embedder: MatchingEmbedder) -> None:
        self.path = settings.data_dir / "db" / "matching_vectors.db"
        self.dim = embedder.dim
        self.model_version = embedder.model_version

    @contextmanager
    def _store(self, *, write: bool = False) -> Iterator[NumpyStore]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        store = NumpyStore(self.path, dimensions=self.dim)
        try:
            with store.connection:
                store.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield store
        finally:
            store.close()

    def _record(self, store: NumpyStore, id: int, key: str) -> tuple[str] | None:
        row = store.connection.execute(
            "SELECT model_version,metadata_json,namespace FROM pfc_vector_records WHERE chunk_id=?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        if row[2] != "matching" or json.loads(row[1]).get("cache_key") != key:
            raise ValueError("Matching vector cache ID collision; existing vector was not changed")
        return (row[0],)

    def get_vectors(self, owners: list[ChunkOwner]) -> list[list[float] | None]:
        if not owners:
            return []
        result: list[list[float] | None] = []
        with self._store() as store:
            for owner in owners:
                key = _cache_key(owner)
                id = _cache_id(key)
                row = self._record(store, id, key)
                vector = store.get_vector(id) if row and row[0] == self.model_version else None
                result.append(vector.tolist() if vector is not None else None)
        return result

    def upsert(self, owners: list[ChunkOwner], vectors: list[list[float]]) -> None:
        if len(owners) != len(vectors) or any(len(v) != self.dim for v in vectors):
            raise ValueError("Matching cache vectors must match owners and model dimensions")
        if not owners:
            return
        with self._store(write=True) as store:
            for owner, vector in zip(owners, vectors, strict=True):
                key = _cache_key(owner)
                id = _cache_id(key)
                self._record(store, id, key)
                store.upsert(
                    id,
                    np.asarray(vector, dtype=np.float32),
                    self.model_version,
                    namespace="matching",
                    metadata={"cache_key": key},
                )


def resolve_matching_ports(
    settings: Settings, *, cached: MatchingPorts | None = None, llm: LlmProvider | None = None
) -> MatchingPorts:
    with database(settings) as db:
        active = db.execute(
            "SELECT embedding_model_id FROM embedding_models WHERE is_active=1"
        ).fetchone()
    if active is None:
        raise EmbeddingModelUnavailable("no active embedding model")
    row, model, tokenizer = resolve_model_files(settings, embedding_model_id=active[0])
    if not model.is_file() or not tokenizer.is_file():
        raise EmbeddingModelUnavailable(f"embedding model files not found at {model} / {tokenizer}")
    verify_model_file(model, row["file_sha256"])
    try:
        fingerprint = model_fingerprint(model, tokenizer)
    except (OSError, ValueError) as exc:
        raise EmbeddingModelUnavailable(
            f"Cannot read embedding model bundle at {model.parent}"
        ) from exc
    key: ResolutionKey = (
        row["embedding_model_id"],
        row["name"],
        row["runtime"],
        row["file_sha256"],
        row["dim"],
        row["pipeline_version"],
        fingerprint,
    )
    with database(settings) as db:
        current = db.execute("SELECT * FROM embedding_models WHERE is_active=1").fetchone()
    if current is None or tuple(current) != tuple(row):
        raise EmbeddingModelUnavailable("Active embedding model changed during resolution; retry")
    if (
        cached
        and isinstance(cached.embedder, MatchingEmbedder)
        and cached.embedder.resolution_key == key
    ):
        return (
            cached
            if cached.llm is llm
            else MatchingPorts(cached.embedder, llm, cached.vector_store)
        )
    embedder = MatchingEmbedder(row, create_embedder(settings, embedding_model_id=active[0]), key)
    try:
        model_identity(embedder)
    except ValueError as exc:
        raise EmbeddingModelUnavailable(str(exc)) from exc
    with database(settings) as db:
        current = db.execute("SELECT * FROM embedding_models WHERE is_active=1").fetchone()
    if current is None or tuple(current) != tuple(row):
        raise EmbeddingModelUnavailable("Active embedding model changed during resolution; retry")
    return MatchingPorts(embedder, llm, MatchingVectorStore(settings, embedder))
