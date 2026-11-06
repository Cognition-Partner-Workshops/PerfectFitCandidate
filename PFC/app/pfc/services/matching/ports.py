"""Adapter-free service contracts for REQ-MAT-01/02 and REQ-LLM-07."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol


class LlmProvider(Protocol):
    @property
    def model(self) -> str: ...

    def extract_jd(self, text: str) -> dict[str, Any]: ...


class Embedder(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def runtime(self) -> Literal["onnx", "llamacpp"]: ...
    @property
    def file_sha256(self) -> str: ...
    @property
    def dim(self) -> int: ...
    @property
    def pipeline_version(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class ChunkOwner:
    """Cache identity includes model, pipeline and text hash to prevent stale reuse."""

    owner_type: str
    owner_id: int
    section: str
    chunk_no: int
    text_sha256: str
    model_key: str


class VectorStore(Protocol):
    def get_vectors(self, owners: list[ChunkOwner]) -> list[list[float] | None]: ...

    def upsert(self, owners: list[ChunkOwner], vectors: list[list[float]]) -> None: ...


@dataclass(frozen=True)
class MatchingPorts:
    embedder: Embedder | None = None
    llm: LlmProvider | None = None
    vector_store: VectorStore | None = None
