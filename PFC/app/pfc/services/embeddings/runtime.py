"""Construct approved ONNX embedders from local files only (REQ-LLM-07)."""

import hashlib
import json
import sqlite3
from pathlib import Path

from pfc.adapters.vector.embeddings import OnnxEmbedder
from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.embeddings.chunking import pipeline_version


class EmbeddingModelUnavailable(RuntimeError):
    pass


def verify_model_file(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise EmbeddingModelUnavailable(
            f"Local embedding model is missing: {path}. No downloads are performed."
        )
    try:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise EmbeddingModelUnavailable(f"Cannot read local embedding model: {path}") from exc
    if expected_sha256 and digest != expected_sha256:
        raise EmbeddingModelUnavailable(
            f"Embedding model SHA-256 does not match its approved row: {path}"
        )


def resolve_model_files(
    settings: Settings, *, embedding_model_id: int
) -> tuple[sqlite3.Row, Path, Path]:
    with database(settings) as db:
        row = db.execute(
            "SELECT * FROM embedding_models WHERE embedding_model_id=?", (embedding_model_id,)
        ).fetchone()
    if row is None:
        raise EmbeddingModelUnavailable(f"Embedding model {embedding_model_id} is not registered")
    if row["runtime"] != "onnx":
        raise EmbeddingModelUnavailable("The re-embedding service requires a local ONNX model")
    bundled = settings.bundled_models_dir / "nomic-embed-text-v1.5"
    model_path = bundled / "model_int8.onnx"
    for path in sorted((settings.data_dir / "models" / "receipts").glob("v*.json")):
        try:
            receipt = json.loads(path.read_bytes())
            if receipt["model_type"] == "embedding" and receipt["model_id"] == embedding_model_id:
                model_path = Path(receipt["path"])
                break
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EmbeddingModelUnavailable(f"Invalid local model receipt: {path}") from exc
    tokenizer_path = model_path.parent / "tokenizer.json"
    if not tokenizer_path.is_file():
        tokenizer_path = bundled / "tokenizer.json"
    return row, model_path, tokenizer_path


def create_embedder(settings: Settings, *, embedding_model_id: int) -> OnnxEmbedder:
    row, model_path, tokenizer_path = resolve_model_files(
        settings, embedding_model_id=embedding_model_id
    )
    verify_model_file(model_path, row["file_sha256"])
    if not tokenizer_path.is_file():
        raise EmbeddingModelUnavailable(
            f"Local embedding tokenizer is missing: {tokenizer_path}. No downloads are performed."
        )
    try:
        embedder = OnnxEmbedder(model_path, tokenizer_path, dimensions=row["dim"], batch_size=32)
    except Exception as exc:
        raise EmbeddingModelUnavailable(
            f"Cannot load local ONNX embedding model: {model_path}"
        ) from exc
    embedder.model_version += f":pipeline={pipeline_version(row['pipeline_version'])}"
    return embedder
