"""Transactional, model-isolated cosine stores (REQ-MAT-01, REQ-LLM-07)."""

from __future__ import annotations

import json
import re
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
    def get_vector(self, id: int) -> Vectors | None: ...


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
    atomically persist domain rows and vectors. Dimensions are fixed per model version.
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
            "SELECT min(length(vector)), max(length(vector)) FROM pfc_vector_records "
            "GROUP BY model_version"
        ).fetchall()
        if any(row[0] != row[1] or row[0] < 4 or row[0] % 4 for row in lengths):
            self.close()
            raise ValueError("Stored vectors have inconsistent dimensions within a model version")

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

    def _model_dimensions(self, model_version: str) -> int | None:
        row = self.connection.execute(
            "SELECT length(vector) FROM pfc_vector_records WHERE model_version=? LIMIT 1",
            (model_version,),
        ).fetchone()
        return int(row[0]) // 4 if row else None

    def _vector(self, vector: Vectors, model_version: str) -> Vectors:
        result = np.asarray(vector, dtype=np.float32)
        dimensions = self._model_dimensions(model_version)
        if result.ndim != 1 or not result.size or not np.all(np.isfinite(result)):
            raise ValueError("Vector must be a finite, nonempty vector")
        if dimensions is not None and result.size != dimensions:
            raise ValueError("Vector must match model version dimensions")
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
        encoded = json.dumps(metadata or {}, allow_nan=False)
        with self._atomic():
            blob = self._vector(vector, model_version).astype("<f4").tobytes()
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
        query = self._vector(vector, model_version)
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

    def get_vector(self, id: int) -> Vectors | None:
        row = self.connection.execute(
            "SELECT vector FROM pfc_vector_records WHERE chunk_id=?", (id,)
        ).fetchone()
        return np.frombuffer(row[0], dtype="<f4").copy() if row else None

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()


class SqliteVecStore(NumpyStore):
    """Dimension-specific vec tables share domain chunk ids (REQ-MAT-01/LLM-07)."""

    def __init__(
        self, source: sqlite3.Connection | str | Path = ":memory:", *, dimensions: int = 768
    ) -> None:
        super().__init__(source, dimensions=dimensions)
        try:
            load_extension(self.connection)
            with self._atomic():
                sizes = {dimensions} | {
                    int(row[0]) // 4
                    for row in self.connection.execute(
                        "SELECT DISTINCT length(vector) FROM pfc_vector_records"
                    )
                }
                for size in sorted(sizes):
                    table = self._table(size)
                    self.connection.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        "(SELECT chunk_id FROM pfc_vector_records)"
                    )
                    self.connection.execute(
                        f"INSERT INTO {table}(rowid, embedding) "
                        "SELECT chunk_id, vector FROM pfc_vector_records WHERE length(vector)=?",
                        (size * 4,),
                    )
        except BaseException:
            self.close()
            raise

    def _table(self, dimensions: int) -> str:
        table = f"vec_chunks_d{dimensions}"
        self.connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{dimensions}])"
        )
        return table

    def _update_index(self, id: int, blob: bytes | None) -> None:
        tables = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name='vec_chunks' OR name GLOB 'vec_chunks_d*')"
        ).fetchall()
        for (name,) in tables:
            if name == "vec_chunks" or re.fullmatch(r"vec_chunks_d[1-9][0-9]*", name):
                self.connection.execute(f"DELETE FROM {name} WHERE rowid=?", (id,))
        if blob is not None:
            table = self._table(len(blob) // 4)
            self.connection.execute(
                f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)", (id, blob)
            )

    def search(
        self, vector: Vectors, *, model_version: str, top_k: int = 10, namespace: str | None = None
    ) -> list[SearchResult]:
        query_vector = self._vector(vector, model_version)
        query = query_vector.astype("<f4").tobytes()
        if not model_version or top_k < 0:
            raise ValueError("Model version and nonnegative top_k required")
        if self._model_dimensions(model_version) is None:
            return []
        table = self._table(len(query_vector))
        rows = self.connection.execute(
            "SELECT r.chunk_id, 1 - vec_distance_cosine(v.embedding, ?), "
            f"r.model_version, r.metadata_json, r.namespace FROM {table} v "
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
