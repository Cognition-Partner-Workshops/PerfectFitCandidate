"""Transactional, model-isolated cosine stores (REQ-MAT-01, REQ-LLM-07)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import sqlite_vec  # type: ignore[import-untyped]

from pfc.adapters.vector.embeddings import Vectors


@dataclass(frozen=True)
class SearchResult:
    id: int
    score: float
    model_version: str
    metadata: dict[str, Any]
    namespace: str


class VectorStore(Protocol):
    """Model version is mandatory on queries: never mix models (REQ-LLM-07)."""

    def upsert(
        self,
        id: int,
        vector: Vectors,
        model_version: str,
        *,
        metadata: dict[str, Any] | None = None,
        namespace: str = "",
    ) -> None: ...
    def delete(self, id: int) -> None: ...
    def search(
        self, vector: Vectors, *, model_version: str, top_k: int = 10, namespace: str | None = None
    ) -> list[SearchResult]: ...
    def count(self, *, model_version: str | None = None) -> int: ...
    def get_model_version(self, id: int) -> str | None: ...


class ExtensionUnavailable(RuntimeError):
    pass


def load_extension(connection: sqlite3.Connection) -> None:
    try:
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
    except (sqlite3.Error, AttributeError) as exc:
        raise ExtensionUnavailable("sqlite-vec extension cannot be loaded") from exc


class NumpyStore:
    """SQLite BLOB persistence with exhaustive NumPy cosine search (REQ-MAT-01).

    pfc_vector_records holds adapter data; domain embedding_chunks/embedding_models
    remain the caller's responsibility. Pass a connection inside a transaction to
    atomically persist domain rows and vectors. Dimensions are fixed per store.
    """

    def __init__(
        self, source: sqlite3.Connection | str | Path = ":memory:", *, dimensions: int = 768
    ) -> None:
        if dimensions < 1:
            raise ValueError("Dimensions must be positive")
        self.dimensions = dimensions
        self._owns_connection = not isinstance(source, sqlite3.Connection)
        self.connection = (
            source if isinstance(source, sqlite3.Connection) else sqlite3.connect(source)
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS pfc_vector_records ("
            "chunk_id INTEGER PRIMARY KEY, vector BLOB NOT NULL, model_version TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL, namespace TEXT NOT NULL)"
        )
        lengths = self.connection.execute(
            "SELECT DISTINCT length(vector) FROM pfc_vector_records"
        ).fetchall()
        if any(row[0] != dimensions * 4 for row in lengths):
            self.close()
            raise ValueError("Stored vectors have different dimensions")

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        self.connection.execute("SAVEPOINT pfc_vector_write")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK TO pfc_vector_write")
            self.connection.execute("RELEASE pfc_vector_write")
            raise
        self.connection.execute("RELEASE pfc_vector_write")

    def _vector(self, vector: Vectors) -> Vectors:
        result = np.asarray(vector, dtype=np.float32)
        if result.shape != (self.dimensions,) or not np.all(np.isfinite(result)):
            raise ValueError("Vector must be finite and match store dimensions")
        norm = float(np.linalg.norm(result.astype(np.float64)))
        if norm == 0:
            raise ValueError("Cosine similarity requires a nonzero vector")
        return np.asarray(result / norm, dtype=np.float32)

    def upsert(
        self,
        id: int,
        vector: Vectors,
        model_version: str,
        *,
        metadata: dict[str, Any] | None = None,
        namespace: str = "",
    ) -> None:
        if not model_version:
            raise ValueError("Model version is required")
        blob = self._vector(vector).astype("<f4").tobytes()
        encoded = json.dumps(metadata or {}, allow_nan=False)
        with self._atomic():
            self.connection.execute(
                "INSERT INTO pfc_vector_records VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET vector=excluded.vector, "
                "model_version=excluded.model_version, metadata_json=excluded.metadata_json, "
                "namespace=excluded.namespace",
                (id, blob, model_version, encoded, namespace),
            )
            self._update_index(id, blob)

    def _update_index(self, id: int, blob: bytes | None) -> None:
        pass

    def delete(self, id: int) -> None:
        with self._atomic():
            self.connection.execute("DELETE FROM pfc_vector_records WHERE chunk_id=?", (id,))
            self._update_index(id, None)

    def _rows(self, model_version: str, namespace: str | None) -> list[Any]:
        return self.connection.execute(
            "SELECT chunk_id, vector, model_version, metadata_json, namespace "
            "FROM pfc_vector_records WHERE model_version=? AND (? IS NULL OR namespace=?)",
            (model_version, namespace, namespace),
        ).fetchall()

    def search(
        self, vector: Vectors, *, model_version: str, top_k: int = 10, namespace: str | None = None
    ) -> list[SearchResult]:
        query = self._vector(vector)
        if not model_version or top_k < 0:
            raise ValueError("Model version and nonnegative top_k required")
        rows = self._rows(model_version, namespace)
        results = [
            SearchResult(
                row[0],
                float(np.clip(np.dot(query, np.frombuffer(row[1], dtype="<f4")), -1, 1)),
                row[2],
                json.loads(row[3]),
                row[4],
            )
            for row in rows
        ]
        return sorted(results, key=lambda result: (-result.score, result.id))[:top_k]

    def count(self, *, model_version: str | None = None) -> int:
        return int(
            self.connection.execute(
                "SELECT count(*) FROM pfc_vector_records WHERE ? IS NULL OR model_version=?",
                (model_version, model_version),
            ).fetchone()[0]
        )

    def get_model_version(self, id: int) -> str | None:
        row = self.connection.execute(
            "SELECT model_version FROM pfc_vector_records WHERE chunk_id=?", (id,)
        ).fetchone()
        return str(row[0]) if row else None

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()


class SqliteVecStore(NumpyStore):
    """vec_chunks rowids are chunk ids, as in TDD §4.9 (REQ-MAT-01)."""

    def __init__(
        self, source: sqlite3.Connection | str | Path = ":memory:", *, dimensions: int = 768
    ) -> None:
        super().__init__(source, dimensions=dimensions)
        try:
            load_extension(self.connection)
            self.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks "
                f"USING vec0(embedding float[{dimensions}])"
            )
            with self._atomic():
                self.connection.execute(
                    "DELETE FROM vec_chunks WHERE rowid IN "
                    "(SELECT chunk_id FROM pfc_vector_records)"
                )
                self.connection.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) "
                    "SELECT chunk_id, vector FROM pfc_vector_records"
                )
        except BaseException:
            self.close()
            raise

    def _update_index(self, id: int, blob: bytes | None) -> None:
        self.connection.execute("DELETE FROM vec_chunks WHERE rowid=?", (id,))
        if blob is not None:
            self.connection.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (id, blob)
            )

    def search(
        self, vector: Vectors, *, model_version: str, top_k: int = 10, namespace: str | None = None
    ) -> list[SearchResult]:
        query = self._vector(vector).astype("<f4").tobytes()
        if not model_version or top_k < 0:
            raise ValueError("Model version and nonnegative top_k required")
        rows = self.connection.execute(
            "SELECT r.chunk_id, 1 - vec_distance_cosine(v.embedding, ?), "
            "r.model_version, r.metadata_json, r.namespace FROM vec_chunks v "
            "JOIN pfc_vector_records r ON r.chunk_id=v.rowid "
            "WHERE r.model_version=? AND (? IS NULL OR r.namespace=?) "
            "ORDER BY 2 DESC, r.chunk_id LIMIT ?",
            (query, model_version, namespace, namespace, top_k),
        ).fetchall()
        return [
            SearchResult(r[0], float(np.clip(r[1], -1, 1)), r[2], json.loads(r[3]), r[4])
            for r in rows
        ]


def create_vector_store(
    source: sqlite3.Connection | str | Path = ":memory:", *, dimensions: int = 768
) -> NumpyStore:
    """Fall back only on unavailable extension, not schema/data errors (REQ-MAT-01)."""
    try:
        return SqliteVecStore(source, dimensions=dimensions)
    except ExtensionUnavailable:
        return NumpyStore(source, dimensions=dimensions)
