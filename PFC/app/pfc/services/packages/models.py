"""Signed model bundles and incremental local re-embedding (REQ-OCC-03/LLM-07)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pfc.adapters.vector.stores import create_vector_store
from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import connect, database
from pfc.jobs.runner import JobContext
from pfc.security import package_signing as signing
from pfc.services.embeddings.reembed import reembed
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable, create_embedder
from pfc.services.packages.common import (
    PackageServiceError,
    app_version,
    atomic_write,
    package_lock,
    timestamp,
    transaction,
)
from pfc.services.packages.keys import load_trusted_keys

METADATA = "model.v1.json"


class JobSubmitter(Protocol):
    def submit(
        self,
        kind: str,
        fn: Callable[[JobContext], None],
        params: dict[str, Any] | None = None,
        created_by: int | None = None,
    ) -> int: ...


class ModelMetadata(BaseModel):
    """Signed model.v1.json accompanies artifacts pinned by the manifest (REQ-OCC-03)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    model_type: Literal["embedding", "llm"]
    name: str = Field(min_length=1, max_length=200)
    file: str = Field(min_length=1)
    runtime: Literal["onnx", "llamacpp"]
    licence: str = Field(min_length=1)
    dim: int = Field(default=768, ge=1, le=65536)
    pipeline_version: str = Field(default="v1", min_length=1)
    ram_estimate_mb: int = Field(default=0, ge=0)
    context_tokens: int = Field(default=8192, ge=1)


@dataclass(frozen=True)
class ModelPreview:
    manifest: signing.Manifest
    metadata: ModelMetadata


@dataclass(frozen=True)
class ModelApplyResult:
    version: int
    model_id: int
    path: Path
    job_id: int


@contextmanager
def _stream(source: Path | bytes) -> Iterator[BinaryIO]:
    stream = source.open("rb") if isinstance(source, Path) else io.BytesIO(source)
    try:
        yield stream
    finally:
        stream.close()


def _llm_placeholder(context: JobContext) -> None:
    context.report_progress(0, "Re-embedding placeholder; existing vectors are retained")
    context.report_progress(1, "Placeholder complete; no vectors were changed")


def _embedding_job(settings: Settings, model_id: int, version: int, context: JobContext) -> None:
    connection = connect(settings)
    try:
        try:
            embedder = create_embedder(settings, embedding_model_id=model_id)
            store = create_vector_store(connection, dimensions=embedder.dimensions)
            counts = reembed(
                settings, context, embedding_model_id=model_id, embedder=embedder, store=store
            )
        except EmbeddingModelUnavailable as exc:
            message = (
                f"{exc}; install the approved model/tokenizer files and "
                f"POST /occ/embedding-models/{version}/reembed"
            )
            with database(settings) as db:
                db.execute(
                    "UPDATE jobs SET status='failed',message=?,finished_at=? WHERE job_id=?",
                    (message, timestamp(), context.job_id),
                )
            return
        message = (
            f"Re-embedding complete: embedded={counts.embedded}, reused={counts.reused}, "
            f"skipped={counts.skipped}, deleted={counts.deleted}"
        )
        context.report_progress(1, message)
        with database(settings) as db:
            db.execute(
                "UPDATE jobs SET status='completed',progress=1,message=?,finished_at=? "
                "WHERE job_id=?",
                (message, timestamp(), context.job_id),
            )
    finally:
        connection.close()


class ModelImporter:
    def __init__(self, settings: Settings, *, jobs: JobSubmitter | None = None) -> None:
        self.settings, self.jobs = settings, jobs

    @property
    def _receipts(self) -> Path:
        return self.settings.data_dir / "models" / "receipts"

    def _current_version(self) -> int:
        return max((int(path.stem[1:]) for path in self._receipts.glob("v*.json")), default=0)

    def _verify(self, stream: BinaryIO) -> ModelPreview:
        verified = signing.verify_package(
            stream,
            trusted_keys=load_trusted_keys(self.settings),
            expected_kind="model",
            app_version=app_version(),
            current_version=0,
        )
        manifest = verified.manifest
        if manifest.package_version <= self._current_version():
            receipt = self._receipts / f"v{manifest.package_version}.json"
            if receipt.is_file() and json.loads(receipt.read_bytes())["job_id"] is None:
                raise PackageServiceError(
                    "model_installed_pending", "Model already installed; retry re-embedding"
                )
            raise signing.PackageError(
                signing.Reason.STALE_VERSION, "Model version must be newer than installed"
            )
        if METADATA not in manifest.payloads or manifest.payloads[METADATA].size > 65536:
            raise PackageServiceError("model_metadata", "Missing/oversized signed model metadata")
        with zipfile.ZipFile(stream) as archive:
            raw_metadata = archive.read(METADATA)
            if hashlib.sha256(raw_metadata).hexdigest() != manifest.payloads[METADATA].sha256:
                raise PackageServiceError(
                    "model_metadata", "Model metadata changed during verification"
                )
            try:
                metadata = ModelMetadata.model_validate_json(raw_metadata)
            except ValidationError as exc:
                raise PackageServiceError(
                    "model_metadata", "Invalid signed model metadata"
                ) from exc
        if metadata.file not in manifest.payloads or metadata.file == METADATA:
            raise PackageServiceError(
                "model_metadata", "Selected model is absent from the signed bundle"
            )
        return ModelPreview(manifest, metadata)

    def preview(self, source: Path | bytes) -> ModelPreview:
        with _stream(source) as stream:
            return self._verify(stream)

    def apply(
        self, source: Path | bytes, *, actor_id: int, expected_sha256: str | None = None
    ) -> ModelApplyResult:
        if self.jobs is None:
            raise PackageServiceError(
                "job_runner_required", "Model application requires a job runner"
            )
        with package_lock(self.settings), _stream(source) as stream:
            if expected_sha256 is not None:
                digest = hashlib.sha256()
                while block := stream.read(signing.CHUNK_SIZE):
                    digest.update(block)
                if digest.hexdigest() != expected_sha256:
                    raise PackageServiceError("inbox_changed", "Pending model package changed")
                stream.seek(0)
            preview = self._verify(stream)
            version, metadata = preview.manifest.package_version, preview.metadata
            root = self.settings.data_dir / "models"
            root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".pfc-", dir=root))
            destination = root / f"v{version}"
            receipt_path = self._receipts / f"v{version}.json"
            installed = False
            receipt_written = False
            try:
                with zipfile.ZipFile(stream) as archive:
                    for name, pin in preview.manifest.payloads.items():
                        if name == METADATA:
                            continue
                        path = staging / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        with archive.open(name) as member, path.open("xb") as output:
                            while block := member.read(signing.CHUNK_SIZE):
                                digest.update(block)
                                output.write(block)
                            output.flush()
                            os.fsync(output.fileno())
                        if digest.hexdigest() != pin.sha256:
                            raise PackageServiceError(
                                "model_hash", "Model changed while being copied"
                            )
                tokenizer_pin = preview.manifest.payloads.get("tokenizer.json")
                adjacent = Path(metadata.file).parent / "tokenizer.json"
                if tokenizer_pin is not None and adjacent != Path("tokenizer.json"):
                    adjacent_pin = preview.manifest.payloads.get(adjacent.as_posix())
                    if adjacent_pin is not None and adjacent_pin.sha256 != tokenizer_pin.sha256:
                        raise PackageServiceError(
                            "model_tokenizer", "Root and adjacent tokenizers must be identical"
                        )
                    atomic_write(staging / adjacent, (staging / "tokenizer.json").read_bytes())
                if destination.exists() or receipt_path.exists():
                    raise PackageServiceError(
                        "version_collision", "Model bundle version already exists"
                    )
                model_path = destination / metadata.file
                pin = preview.manifest.payloads[metadata.file]
                with transaction(self.settings) as db:
                    if metadata.model_type == "embedding":
                        db.execute(
                            "INSERT INTO embedding_models(name, runtime, file_sha256, dim, "
                            "normalised, pipeline_version, is_active) VALUES (?, ?, ?, ?, 1, ?, 0) "
                            "ON CONFLICT(name, file_sha256, pipeline_version) DO NOTHING",
                            (
                                f"{metadata.name}@v{version}",
                                metadata.runtime,
                                pin.sha256,
                                metadata.dim,
                                metadata.pipeline_version,
                            ),
                        )
                        model_id = int(
                            db.execute(
                                "SELECT embedding_model_id FROM embedding_models "
                                "WHERE name=? AND file_sha256=? AND pipeline_version=?",
                                (
                                    f"{metadata.name}@v{version}",
                                    pin.sha256,
                                    metadata.pipeline_version,
                                ),
                            ).fetchone()[0]
                        )
                    else:
                        cursor = db.execute(
                            "INSERT INTO llm_models(name, file_name, file_sha256, "
                            "size_mb, ram_estimate_mb, context_tokens, licence, approved, "
                            "is_active) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)",
                            (
                                f"{metadata.name}@v{version}",
                                str(model_path),
                                pin.sha256,
                                (pin.size + 1024**2 - 1) // 1024**2,
                                metadata.ram_estimate_mb,
                                metadata.context_tokens,
                                metadata.licence,
                            ),
                        )
                        model_id = int(cursor.lastrowid or 0)
                    append_audit(
                        db,
                        ts=timestamp(),
                        action="model_package.import",
                        user_id=actor_id,
                        entity_type=f"{metadata.model_type}_model",
                        entity_id=str(model_id),
                        details={
                            "package_version": version,
                            "file_sha256": pin.sha256,
                            "signing_key_id": preview.manifest.key_id,
                        },
                    )
                    staging.rename(destination)
                    installed = True
                    atomic_write(
                        receipt_path,
                        signing.canonical_json(
                            {
                                "package_version": version,
                                "model_id": model_id,
                                "model_type": metadata.model_type,
                                "path": str(model_path),
                                "file_sha256": pin.sha256,
                                "bundle_sha256": hashlib.sha256(
                                    signing.canonical_json(
                                        {
                                            name: pin.model_dump()
                                            for name, pin in preview.manifest.payloads.items()
                                        }
                                    )
                                ).hexdigest(),
                                "job_id": None,
                            }
                        ),
                    )
                    receipt_written = True
            except BaseException:
                if installed:
                    shutil.rmtree(destination)
                if receipt_written:
                    receipt_path.unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        job_id = self.retry_reembedding(version, actor_id=actor_id)
        return ModelApplyResult(version, model_id, model_path, job_id)

    def retry_reembedding(self, version: int, *, actor_id: int) -> int:
        """Deduplicate live jobs; terminal jobs resume through a fresh run (REQ-OCC-03)."""
        if self.jobs is None:
            raise PackageServiceError("job_runner_required", "A job runner is required")
        with package_lock(self.settings):
            path = self._receipts / f"v{version}.json"
            receipt = json.loads(path.read_bytes())
            if receipt["job_id"] is not None:
                with database(self.settings) as db:
                    job = db.execute(
                        "SELECT status FROM jobs WHERE job_id=?", (receipt["job_id"],)
                    ).fetchone()
                if job is not None and job["status"] in ("queued", "running"):
                    return int(receipt["job_id"])

            def run(context: JobContext) -> None:
                _embedding_job(self.settings, receipt["model_id"], version, context)

            try:
                job_id = self.jobs.submit(
                    "embed",
                    run if receipt["model_type"] == "embedding" else _llm_placeholder,
                    params={
                        "model_id": receipt["model_id"],
                        "model_type": receipt["model_type"],
                        "package_version": version,
                    },
                    created_by=actor_id,
                )
            except Exception as exc:
                raise PackageServiceError(
                    "reembedding_pending",
                    f"Model v{version} imported; job submission failed. Explicit retry is required",
                ) from exc
            receipt["job_id"] = job_id
            atomic_write(path, signing.canonical_json(receipt))
            return job_id
