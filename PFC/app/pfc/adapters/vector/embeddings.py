"""Offline, versioned nomic embeddings (REQ-LLM-07, REQ-MAT-01/08)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol

import numpy as np
import onnxruntime as ort  # type: ignore[import-untyped]
from numpy.typing import NDArray
from tokenizers import Tokenizer

Task = Literal["document", "query"]
Vectors = NDArray[np.float32]


class Embedder(Protocol):
    """Callers persist model_version alongside every vector (REQ-LLM-07)."""

    model_version: str
    dimensions: int

    def embed(self, texts: list[str], *, task: Task = "document") -> Vectors: ...


def normalise(vectors: Vectors, dimensions: int | None = None) -> Vectors:
    """Matryoshka: truncate, then L2 normalise (REQ-LLM-07)."""
    if vectors.ndim != 2:
        raise ValueError("Expected a matrix of vectors")
    if dimensions is not None:
        if not 1 <= dimensions <= vectors.shape[1]:
            raise ValueError("Invalid truncation dimensions")
        vectors = vectors[:, :dimensions]
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms != 0)


def mean_pool(hidden: Vectors, attention_mask: NDArray[np.int64]) -> Vectors:
    """Exclude padding from mean pooling (REQ-LLM-07)."""
    if hidden.ndim != 3 or attention_mask.shape != hidden.shape[:2]:
        raise ValueError("Hidden states and attention mask shapes disagree")
    mask = attention_mask.astype(np.float32)[..., None]
    sums = (hidden * mask).sum(axis=1)
    counts = mask.sum(axis=1)
    return np.divide(sums, counts, out=np.zeros_like(sums), where=counts != 0)


def task_prefix(task: Task) -> str:
    if task not in ("document", "query"):
        raise ValueError("Task must be document or query")
    return f"search_{task}: "


class FakeEmbedder:
    """Stable hash-derived unit vectors; no model or network (REQ-LLM-07)."""

    def __init__(self, dimensions: int = 768) -> None:
        if dimensions < 1:
            raise ValueError("Dimensions must be positive")
        self.dimensions = dimensions
        self.model_version = f"fake-shake256-v1:{dimensions}"

    def embed(self, texts: list[str], *, task: Task = "document") -> Vectors:
        prefix = task_prefix(task)
        rows = [
            np.frombuffer(
                hashlib.shake_256((prefix + text).encode()).digest(self.dimensions * 4), dtype="<u4"
            ).astype(np.float32)
            / np.float32(2**31)
            - 1
            for text in texts
        ]
        return normalise(np.asarray(rows, dtype=np.float32).reshape(-1, self.dimensions))


def model_fingerprint(model_path: Path, tokenizer_path: Path) -> str:
    """Hash a self-contained model directory, including external tensors (REQ-LLM-07).

    Keep each approved model bundle in its own directory. All regular files,
    including nested external-data files with arbitrary names, affect the version.
    The tokenizer may live outside the bundle. Symlinks are not supported.
    """
    if model_path.is_symlink() or tokenizer_path.is_symlink():
        raise ValueError("Model bundles must not contain symlinks")
    model_path = model_path.resolve(strict=True)
    tokenizer_path = tokenizer_path.resolve(strict=True)
    if not model_path.is_file() or not tokenizer_path.is_file():
        raise ValueError("Model and tokenizer must be regular files")
    root = model_path.parent
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Model bundles must not contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Model bundles must contain only regular files")
        files[f"bundle/{path.relative_to(root).as_posix()}"] = path
    files["selected-model"] = model_path
    files["selected-tokenizer"] = tokenizer_path
    digest = hashlib.sha256(b"pfc-model-bundle-v1\x00")
    for name, path in sorted(files.items()):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        content_hash = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                content_hash.update(block)
        digest.update(content_hash.digest())
    return digest.hexdigest()


class OnnxEmbedder:
    """CPU-only local nomic-embed-text-v1.5; never downloads (REQ-LLM-07)."""

    def __init__(
        self,
        model_path: str | Path,
        tokenizer_path: str | Path,
        *,
        dimensions: int = 768,
        batch_size: int = 32,
        max_tokens: int = 8192,
    ) -> None:
        if not 1 <= dimensions <= 768 or batch_size < 1 or not 1 <= max_tokens <= 8192:
            raise ValueError("Invalid embedding dimensions, batch size or token limit")
        model_path, tokenizer_path = Path(model_path), Path(tokenizer_path)
        self.dimensions = dimensions
        self._batch_size = batch_size
        self._lock = Lock()
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_padding()
        self._tokenizer.enable_truncation(max_length=max_tokens)
        fingerprint = model_fingerprint(model_path, tokenizer_path)
        self.model_version = (
            f"nomic-embed-text-v1.5:{fingerprint}:mean-l2-v1:d{dimensions}:t{max_tokens}"
        )
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])

    def embed(self, texts: list[str], *, task: Task = "document") -> Vectors:
        prefix = task_prefix(task)
        if not texts:
            return np.empty((0, self.dimensions), dtype=np.float32)
        batches: list[Vectors] = []
        with self._lock:
            for start in range(0, len(texts), self._batch_size):
                encodings = self._tokenizer.encode_batch(
                    [prefix + text for text in texts[start : start + self._batch_size]]
                )
                inputs = {
                    "input_ids": np.asarray([e.ids for e in encodings], dtype=np.int64),
                    "attention_mask": np.asarray(
                        [e.attention_mask for e in encodings], dtype=np.int64
                    ),
                    "token_type_ids": np.asarray([e.type_ids for e in encodings], dtype=np.int64),
                }
                feed = {i.name: inputs[i.name] for i in self._session.get_inputs()}
                hidden = np.asarray(self._session.run(None, feed)[0], dtype=np.float32)
                pooled = mean_pool(hidden, inputs["attention_mask"])
                batches.append(normalise(pooled, self.dimensions))
        return np.concatenate(batches)
