"""Exhaustive semantic inputs; vector retrieval is a cache, not top-k (TDD §6.7)."""

import hashlib
import math
from collections.abc import Callable

from pfc.domain.config_loader import ScoringTemplate
from pfc.domain.scoring import Demand, SemanticInput, SkillBasis, normalise_cosine

from .common import digest
from .ports import ChunkOwner, Embedder, MatchingPorts, Task


def model_identity(embedder: Embedder) -> dict[str, str | int]:
    if embedder.runtime not in {"onnx", "llamacpp"} or embedder.dim <= 0:
        raise ValueError("Invalid embedding model identity")
    if not all((embedder.name, embedder.file_sha256, embedder.pipeline_version)):
        raise ValueError("Embedding model identity must be complete")
    return {
        "name": embedder.name,
        "runtime": embedder.runtime,
        "file_sha256": embedder.file_sha256,
        "dim": embedder.dim,
        "pipeline_version": embedder.pipeline_version,
    }


def _validate(vector: list[float], dim: int) -> None:
    if len(vector) != dim or any(not math.isfinite(v) for v in vector):
        raise ValueError("Embedder returned an invalid vector dimension or nonfinite value")
    if not any(vector):
        raise ValueError("Embedder returned a zero vector")


def _cosine(a: list[float], b: list[float]) -> float:
    denominator = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / denominator if denominator else 0


def _mean(vectors: list[list[float]]) -> list[float]:
    return [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]


class VectorCache:
    def __init__(self, ports: MatchingPorts) -> None:
        if ports.embedder is None:
            raise ValueError("An embedder must be configured to start matching")
        self.ports, self.embedder = ports, ports.embedder
        self.model_key = digest(
            {
                "identity": model_identity(self.embedder),
                "model_version": getattr(self.embedder, "model_version", None),
            }
        )
        self.local: dict[ChunkOwner, list[float]] = {}

    def vectors(
        self, owner_type: str, owner_id: int, texts: list[str], *, task: Task = "document"
    ) -> list[list[float]]:
        if task not in ("document", "query"):
            raise ValueError("Invalid matching embedding task")
        owners = [
            ChunkOwner(
                owner_type,
                owner_id,
                "matching",
                i,
                hashlib.sha256(f"search_{task}: {text}".encode()).hexdigest(),
                self.model_key,
                task,
            )
            for i, text in enumerate(texts)
        ]
        missing = [owner for owner in owners if owner not in self.local]
        if missing and self.ports.vector_store is not None:
            cached = self.ports.vector_store.get_vectors(missing)
            if len(cached) != len(missing):
                raise ValueError("Vector cache returned the wrong number of vectors")
            for owner, vector in zip(missing, cached, strict=True):
                if vector is not None:
                    _validate(vector, self.embedder.dim)
                    self.local[owner] = vector
        missing = [owner for owner in owners if owner not in self.local]
        if missing:
            text_by_owner = dict(zip(owners, texts, strict=True))
            vectors = self.embedder.embed([text_by_owner[owner] for owner in missing], task=task)
            if len(vectors) != len(missing):
                raise ValueError("Embedder returned the wrong number of vectors")
            for vector in vectors:
                _validate(vector, self.embedder.dim)
            if self.ports.vector_store is not None:
                self.ports.vector_store.upsert(missing, vectors)
            self.local.update(zip(missing, vectors, strict=True))
        return [self.local[owner] for owner in owners]


def semantic_inputs(
    demands: list[Demand],
    metadata: dict[str, dict],
    bases: list[SkillBasis],
    cache: VectorCache,
    template: ScoringTemplate,
    progress: Callable[[float, str], None],
) -> dict[tuple[str, str], SemanticInput]:
    result = {}
    basis_vectors = {}
    for basis in bases:
        owner_type = "ct_skills" if basis.basis_id.startswith("CT:") else "cv"
        owner_id = int(basis.basis_id.split(":")[1])
        texts = [f"Skill: {skill.raw}\nEvidence: {skill.evidence}" for skill in basis.skills]
        basis_vectors[basis.basis_id] = (
            cache.vectors(owner_type, owner_id, texts, task="document") if texts else []
        )
    for i, demand in enumerate(demands):
        meta = metadata[demand.code_requisition]
        jd = cache.vectors(
            "jd", meta["current_version_id"], [meta["jd_text_norm"]], task="document"
        )
        items = {
            item.item_id: cache.vectors("jd_item", int(item.item_id), [item.text], task="query")[0]
            for item in demand.requirements
        }
        for basis in bases:
            vectors = basis_vectors[basis.basis_id]
            if not vectors:
                result[(demand.code_requisition, basis.basis_id)] = SemanticInput()
                continue
            overall = normalise_cosine(_cosine(_mean(jd), _mean(vectors)), template)
            credits = {
                item_id: normalise_cosine(max(_cosine(item, v) for v in vectors), template)
                for item_id, item in items.items()
            }
            result[(demand.code_requisition, basis.basis_id)] = SemanticInput(overall, credits)
        progress((i + 1) / len(demands), demand.code_requisition)
    return result
