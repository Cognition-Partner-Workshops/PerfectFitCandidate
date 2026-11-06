"""Signed model retention, job dispatch and preview-only startup inbox (REQ-OCC-02/03)."""

import asyncio
import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from embeddings.conftest import jd, tiny_bundle

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.jobs.runner import JobContext, JobRunner
from pfc.security import package_signing as signing
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable
from pfc.services.packages.common import PackageServiceError
from pfc.services.packages.inbox import PackageInbox, list_pending, scan_inbox
from pfc.services.packages.keys import SigningKeys
from pfc.services.packages.models import METADATA, ModelImporter

from .conftest import NOW
from .test_settings import exported, rewrite


@contextmanager
def running_jobs(settings: Settings) -> Iterator[JobRunner]:
    runner = JobRunner(settings)
    try:
        yield runner
    finally:
        runner.shutdown()


def finished(runner: JobRunner, job_id: int) -> dict[str, Any]:
    async def wait() -> dict[str, Any]:
        async for event in runner.events(job_id, poll_interval=0.01):
            if event.startswith("event: done"):
                return json.loads(event.split("data: ", 1)[1])
        raise AssertionError("Missing terminal job event")

    async def bounded() -> dict[str, Any]:
        return await asyncio.wait_for(wait(), timeout=20)

    return asyncio.run(bounded())


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "completed"])
def test_retry_uses_persisted_job_state(pair: tuple[Settings, Settings], status: str) -> None:
    source, target = pair
    jobs = Jobs(target)
    importer = ModelImporter(target, jobs=jobs)
    result = importer.apply(model_package(source), actor_id=1)
    with database(target) as db:
        db.execute(
            "UPDATE jobs SET status=?,message='interrupted by restart' WHERE job_id=?",
            (status, result.job_id),
        )
    expected = 1 if status in ("queued", "running") else 2
    assert importer.retry_reembedding(1, actor_id=1) == expected
    receipt = json.loads((target.data_dir / "models/receipts/v1.json").read_bytes())
    assert receipt["job_id"] == expected and len(jobs.calls) == expected


def test_missing_files_fail_job_not_import(
    pair: tuple[Settings, Settings], caplog: pytest.LogCaptureFixture
) -> None:
    source, target = pair
    target.bundled_models_dir = target.data_dir / "missing-models"
    with running_jobs(target) as runner:
        importer = ModelImporter(target, jobs=runner)
        result = importer.apply(model_package(source), actor_id=1)
        job = finished(runner, result.job_id)
        assert job["status"] == "failed"
        assert "tokenizer is missing" in job["message"]
        assert "POST /occ/embedding-models/1/reembed" in job["message"]
        assert result.path.is_file()
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_real_import_and_terminal_retry_are_incremental(
    pair: tuple[Settings, Settings], tmp_path: Path
) -> None:
    source, target = pair
    model = tiny_bundle(tmp_path / "tiny")
    with database(target) as db:
        jd(db, 2, "Python", "Engineer")
    package = model_package(
        source,
        dim=8,
        artifacts={
            "weights/model.onnx": model.read_bytes(),
            "weights/tokenizer.json": (model.parent / "tokenizer.json").read_bytes(),
        },
    )
    with running_jobs(target) as runner:
        importer = ModelImporter(target, jobs=runner)
        result = importer.apply(package, actor_id=1)
        job = finished(runner, result.job_id)
        assert job["status"] == "completed" and "embedded=1" in job["message"]
        retry = importer.retry_reembedding(1, actor_id=1)
        assert retry != result.job_id
        job = finished(runner, retry)
        assert (
            job["status"] == "completed"
            and "embedded=0" in job["message"]
            and "skipped=1" in job["message"]
        )
    with database(target) as db:
        assert (
            db.execute(
                "SELECT is_active FROM embedding_models WHERE embedding_model_id=?",
                (result.model_id,),
            ).fetchone()[0]
            == 1
        )
        vector = db.execute("SELECT model_version FROM pfc_vector_records").fetchone()
        assert vector is not None and "pipeline=" in vector[0]


def test_failed_job_resumes_committed_owner(
    pair: tuple[Settings, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = pair
    model = tiny_bundle(tmp_path / "tiny")
    with database(target) as db:
        jd(db, 2, "Python", "Engineer")
        jd(db, 3, "SQL", "Engineer")
    package = model_package(
        source,
        dim=8,
        artifacts={
            "weights/model.onnx": model.read_bytes(),
            "weights/tokenizer.json": (model.parent / "tokenizer.json").read_bytes(),
        },
    )
    report = JobContext.report_progress

    def interrupt(self: JobContext, fraction: float, message: str = "") -> None:
        report(self, fraction, message)
        if message == "Indexed jd 2":
            raise EmbeddingModelUnavailable("Local model files were removed")

    with running_jobs(target) as runner:
        importer = ModelImporter(target, jobs=runner)
        with monkeypatch.context() as patch:
            patch.setattr(JobContext, "report_progress", interrupt)
            result = importer.apply(package, actor_id=1)
            assert finished(runner, result.job_id)["status"] == "failed"
        with database(target) as db:
            assert db.execute("SELECT count(*) FROM embedding_chunks").fetchone()[0] == 1
        retry = importer.retry_reembedding(1, actor_id=1)
        assert retry != result.job_id
        job = finished(runner, retry)
        assert (
            job["status"] == "completed"
            and "embedded=1" in job["message"]
            and "skipped=1" in job["message"]
        )
        with database(target) as db:
            assert db.execute("SELECT count(*) FROM embedding_chunks").fetchone()[0] == 2


class Jobs:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, dict[str, Any] | None, int | None]] = []
        self.fail = False

    def submit(
        self,
        kind: str,
        fn: Callable[[JobContext], None],
        params: dict[str, Any] | None = None,
        created_by: int | None = None,
    ) -> int:
        if self.fail:
            raise RuntimeError("job queue unavailable")
        self.calls.append((kind, params, created_by))
        with database(self.settings) as db:
            db.execute(
                "INSERT INTO jobs(job_id,kind,status,created_at) VALUES (?,?,'queued',?)",
                (len(self.calls), kind, NOW),
            )
        return len(self.calls)


def model_package(
    source: Settings,
    *,
    version: int = 1,
    model_type: str = "embedding",
    artifacts: dict[str, bytes] | None = None,
    **metadata: Any,
) -> bytes:
    output = io.BytesIO()
    values = {
        "model_type": model_type,
        "name": "nomic",
        "file": "weights/model.onnx",
        "runtime": "onnx",
        "licence": "Apache-2.0",
        "dim": 768,
        **metadata,
    }
    signing.build_model_package(
        {
            METADATA: signing.canonical_json(values),
            "weights/model.onnx": b"test model weights",
            "weights/external.data": b"external tensors",
            "tokenizer.json": b"test tokenizer",
            **(artifacts or {}),
        },
        output,
        private=SigningKeys(source).get_or_create("test signing passphrase"),
        package_version=version,
        min_app_version="0.1.0",
        max_app_version="0.1.0",
        created_at=NOW,
    )
    return output.getvalue()


@pytest.mark.parametrize("model_type", ["embedding", "llm"])
def test_model_apply_retains_older_models_and_vectors(
    pair: tuple[Settings, Settings], model_type: str
) -> None:
    source, target = pair
    with database(target) as db:
        db.execute(
            "INSERT INTO embedding_models VALUES (1, 'old', 'onnx', 'old-hash', 768, 1, 'v1', 1)"
        )
        db.execute(
            "INSERT INTO embedding_chunks VALUES (1, 'cv', 1, 'skills', 0, 'Python', 'hash', "
            "1, 1, ?)",
            (NOW,),
        )
        db.execute(
            "INSERT INTO llm_models VALUES (1, 'old', 'old.gguf', 'hash', 1, 1, 8192, "
            "'Apache-2.0', 1, NULL, 1)"
        )
        old_chunks = [tuple(row) for row in db.execute("SELECT * FROM embedding_chunks")]
    jobs = Jobs(target)
    importer = ModelImporter(target, jobs=jobs)
    package = model_package(source, model_type=model_type)
    preview = importer.preview(package)
    assert preview.metadata.model_type == model_type
    assert not (target.data_dir / "models").exists()
    result = importer.apply(package, actor_id=1)
    assert result.path.read_bytes() == b"test model weights"
    assert (result.path.parent / "external.data").read_bytes() == b"external tensors"
    assert (target.data_dir / "models/v1/tokenizer.json").read_bytes() == b"test tokenizer"
    assert result.job_id == 1
    assert jobs.calls[0][0] == "embed" and jobs.calls[0][2] == 1
    with database(target) as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM embedding_chunks")] == old_chunks
        assert db.execute(
            "SELECT file_sha256, is_active FROM embedding_models WHERE embedding_model_id=1"
        ).fetchone()[:] == ("old-hash", 1)
        assert db.execute("SELECT name, is_active FROM llm_models WHERE model_id=1").fetchone()[
            :
        ] == ("old", 1)
        table = "embedding_models" if model_type == "embedding" else "llm_models"
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 2
        assert verify_chain(db)
    assert importer.retry_reembedding(1, actor_id=1) == 1
    assert len(jobs.calls) == 1
    with pytest.raises(signing.PackageError) as stale:
        importer.apply(package, actor_id=1)
    assert stale.value.reason == signing.Reason.STALE_VERSION
    assert not list((target.data_dir / "models").glob(".pfc-*"))


def test_model_failure_cleanup_and_pending_job_retry(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    jobs = Jobs(target)
    importer = ModelImporter(target, jobs=jobs)
    package = model_package(source)
    with pytest.raises(Exception, match="FOREIGN KEY"):
        importer.apply(package, actor_id=999)
    assert not (target.data_dir / "models/v1").exists()
    assert not (target.data_dir / "models/receipts/v1.json").exists()
    with database(target) as db:
        assert db.execute("SELECT count(*) FROM embedding_models").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    jobs.fail = True
    with pytest.raises(PackageServiceError) as error:
        importer.apply(package, actor_id=1)
    assert error.value.reason == "reembedding_pending"
    assert (target.data_dir / "models/v1/weights/model.onnx").exists()
    receipt = json.loads((target.data_dir / "models/receipts/v1.json").read_bytes())
    assert receipt["job_id"] is None
    jobs.fail = False
    assert importer.retry_reembedding(1, actor_id=1) == 1


def test_external_tensor_update_has_a_new_model_version(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    importer = ModelImporter(target, jobs=Jobs(target))
    first = importer.apply(model_package(source), actor_id=1)
    output = io.BytesIO()
    metadata = {
        "model_type": "embedding",
        "name": "nomic",
        "file": "weights/model.onnx",
        "runtime": "onnx",
        "licence": "Apache-2.0",
    }
    signing.build_model_package(
        {
            METADATA: signing.canonical_json(metadata),
            "weights/model.onnx": b"test model weights",
            "weights/external.data": b"new weights",
        },
        output,
        private=SigningKeys(source).get_or_create("test signing passphrase"),
        package_version=2,
        min_app_version="0.1.0",
        max_app_version="0.1.0",
        created_at=NOW,
    )
    second = importer.apply(output.getvalue(), actor_id=1)
    assert first.model_id != second.model_id
    with database(target) as db:
        assert [
            row[0]
            for row in db.execute("SELECT name FROM embedding_models ORDER BY embedding_model_id")
        ] == ["nomic@v1", "nomic@v2"]


def test_model_tamper_metadata_and_wrong_kind(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    importer = ModelImporter(target, jobs=Jobs(target))
    package = model_package(source)
    tampered = rewrite(package, "weights/model.onnx", b"changed weights")
    with pytest.raises(signing.PackageError) as error:
        importer.preview(tampered)
    assert error.value.reason == signing.Reason.PAYLOAD_HASH
    with pytest.raises(PackageServiceError, match="Selected model"):
        importer.preview(model_package(source, file="absent.onnx"))
    with pytest.raises(PackageServiceError, match="Invalid signed model"):
        importer.preview(model_package(source, model_type="unknown"))
    with pytest.raises(signing.PackageError) as wrong:
        importer.preview(exported(source).path)
    assert wrong.value.reason == signing.Reason.WRONG_KIND
    with pytest.raises(PackageServiceError, match="job runner"):
        ModelImporter(target).apply(package, actor_id=1)


def test_real_llm_job_runner_remains_noop(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    runner = JobRunner(target)
    try:
        result = ModelImporter(target, jobs=runner).apply(
            model_package(source, model_type="llm"), actor_id=1
        )

        async def completed() -> None:
            async for _ in runner.events(result.job_id, poll_interval=0.01):
                pass

        asyncio.run(asyncio.wait_for(completed(), timeout=5))
    finally:
        runner.shutdown()
    job = runner.get(result.job_id)
    assert job is not None and job.status == "completed" and job.progress == 1


def test_startup_inbox_is_preview_only_and_explicit_apply(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    inbox = target.data_dir / "inbox"
    inbox.mkdir()
    settings_file = inbox / "settings.pfcpkg"
    settings_file.write_bytes(exported(source).path.read_bytes())
    (inbox / "model.pfcmodel").write_bytes(model_package(source))
    (inbox / "tampered.pfcpkg").write_bytes(b"not a zip")
    (inbox / "ignored.txt").write_text("ignore")
    pending = scan_inbox(target)
    assert len(pending) == 3
    assert sum(item.valid for item in pending) == 2
    assert {item.kind for item in pending} == {"model", "settings"}
    assert list_pending(target) == pending
    assert not (target.data_dir / "packages").exists()
    assert not (target.data_dir / "models").exists()
    with database(target) as db:
        assert db.execute("SELECT count(*) FROM settings_packages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    service = PackageInbox(target, jobs=Jobs(target))
    assert service.list_pending() == pending
    service.apply_pending(settings_file, actor_id=1)
    assert (target.data_dir / "packages/v1.pfcpkg").exists()
    service.apply_pending(inbox / "model.pfcmodel", actor_id=1)
    assert (target.data_dir / "models/v1/weights/model.onnx").exists()
    with pytest.raises(PackageServiceError, match="valid previewed"):
        service.apply_pending(inbox / "tampered.pfcpkg", actor_id=1)


def test_empty_inbox_and_changed_pending_file(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    assert list_pending(target) == []
    assert scan_inbox(target) == []
    inbox = target.data_dir / "inbox"
    inbox.mkdir()
    path = inbox / "incoming.pfcpkg"
    path.write_bytes(exported(source).path.read_bytes())
    service = PackageInbox(target)
    assert len(service.scan()) == 1
    path.write_bytes(exported(source).path.read_bytes())
    with pytest.raises(PackageServiceError, match="changed"):
        service.apply_pending(path, actor_id=1)
    assert not (target.data_dir / "packages").exists()
