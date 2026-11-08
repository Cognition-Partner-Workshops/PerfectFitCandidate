import hashlib
import json
import logging
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.embeddings import runtime
from pfc.services.embeddings.chunking import pipeline_version


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        bundled_models_dir=tmp_path / "models",
    )
    migrate(settings)
    return settings


def write_bundle(
    settings: Settings, model: bytes = b"model", tokenizer: bytes = b"tokenizer"
) -> Path:
    directory = settings.bundled_models_dir / runtime.BUNDLED_EMBEDDING_NAME
    directory.mkdir(parents=True)
    model_path = directory / "model_int8.onnx"
    model_path.write_bytes(model)
    (directory / "tokenizer.json").write_bytes(tokenizer)
    return model_path


@pytest.mark.parametrize("missing", ["model", "tokenizer"])
def test_missing_bundled_files_do_not_register_model(tmp_path: Path, missing: str) -> None:
    settings = settings_for(tmp_path)
    directory = settings.bundled_models_dir / runtime.BUNDLED_EMBEDDING_NAME
    directory.mkdir(parents=True)
    if missing == "model":
        (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    else:
        (directory / "model_int8.onnx").write_bytes(b"model")

    assert runtime.register_bundled_embedding_model(settings) is None
    with database(settings) as db:
        assert db.execute("SELECT COUNT(*) FROM embedding_models").fetchone()[0] == 0


def test_hash_mismatch_does_not_register_model_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(tmp_path)
    write_bundle(settings, model=b"unexpected")
    monkeypatch.setattr(runtime, "BUNDLED_EMBEDDING_SHA256", hashlib.sha256(b"pinned").hexdigest())

    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        assert runtime.register_bundled_embedding_model(settings) is None

    assert any(
        record.levelno == logging.WARNING and "SHA-256" in record.message
        for record in caplog.records
    )
    with database(settings) as db:
        assert db.execute("SELECT COUNT(*) FROM embedding_models").fetchone()[0] == 0


def test_matching_bundle_registers_once_and_activates_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path)
    model_path = write_bundle(settings)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    monkeypatch.setattr(runtime, "BUNDLED_EMBEDDING_SHA256", digest)
    monkeypatch.setattr(runtime, "BUNDLED_EMBEDDING_DIM", 8)

    model_id = runtime.register_bundled_embedding_model(settings)
    assert model_id is not None
    with database(settings) as db:
        row = db.execute(
            "SELECT name,runtime,file_sha256,dim,normalised,pipeline_version,is_active "
            "FROM embedding_models WHERE embedding_model_id=?",
            (model_id,),
        ).fetchone()
        assert tuple(row) == (
            runtime.BUNDLED_EMBEDDING_NAME,
            "onnx",
            digest,
            8,
            1,
            pipeline_version("v1"),
            1,
        )

    assert runtime.register_bundled_embedding_model(settings) is None
    with database(settings) as db:
        assert db.execute("SELECT COUNT(*) FROM embedding_models").fetchone()[0] == 1


def test_existing_active_embedding_is_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path)
    model_path = write_bundle(settings)
    monkeypatch.setattr(
        runtime, "BUNDLED_EMBEDDING_SHA256", hashlib.sha256(model_path.read_bytes()).hexdigest()
    )
    with database(settings) as db:
        db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,normalised,"
            "pipeline_version,is_active) VALUES ('imported','onnx','imported-hash',8,1,'v1',1)"
        )

    assert runtime.register_bundled_embedding_model(settings) is None
    with database(settings) as db:
        rows = db.execute(
            "SELECT name,is_active FROM embedding_models ORDER BY embedding_model_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("imported", 1)]


def test_bundled_embedding_hash_matches_models_lock() -> None:
    repo_root = Path(__file__).resolve().parents[6]
    models_lock = json.loads((repo_root / "PFC" / "packaging" / "MODELS.lock").read_text())
    model = next(
        item for item in models_lock if item["file"] == "nomic-embed-text-v1.5/model_int8.onnx"
    )

    assert runtime.BUNDLED_EMBEDDING_SHA256 == model["sha256"]
