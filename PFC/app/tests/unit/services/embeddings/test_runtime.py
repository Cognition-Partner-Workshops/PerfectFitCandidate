import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from pfc.adapters.vector.embeddings import OnnxEmbedder
from pfc.adapters.vector.stores import NumpyStore
from pfc.config import Settings
from pfc.jobs.runner import JobContext
from pfc.services.embeddings.chunking import CHUNKER_VERSION
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable, create_embedder

from .conftest import tiny_bundle


@pytest.mark.parametrize("token_types", [False, True])
def test_tiny_real_model_prefixes_and_outputs(tmp_path: Path, token_types: bool) -> None:
    path = tiny_bundle(tmp_path / "tiny", token_types=token_types)
    embedder = OnnxEmbedder(path, path.parent / "tokenizer.json", dimensions=8, batch_size=1)
    document = embedder.embed(["Python"], task="document")
    query = embedder.embed(["Python"], task="query")
    assert document.shape == query.shape == (1, 8)
    assert not np.allclose(document, query)
    np.testing.assert_allclose(np.linalg.norm(document, axis=1), [1], atol=1e-6)
    assert embedder.embed([]).shape == (0, 8)


def test_bundled_models_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(config_dir=tmp_path / "install" / "config")
    assert settings.bundled_models_dir == tmp_path / "install" / "models"
    monkeypatch.setenv("PFC_BUNDLED_MODELS_DIR", str(tmp_path / "override"))
    assert Settings(config_dir=settings.config_dir).bundled_models_dir == tmp_path / "override"


def test_factory_missing_files_and_hash_validation(
    state: tuple[Settings, NumpyStore, JobContext],
) -> None:
    settings, store, _ = state
    with pytest.raises(EmbeddingModelUnavailable, match="model is missing"):
        create_embedder(settings, embedding_model_id=2)
    path = tiny_bundle(settings.bundled_models_dir / "nomic-embed-text-v1.5")
    store.connection.execute(
        "UPDATE embedding_models SET file_sha256='wrong' WHERE embedding_model_id=2"
    )
    store.connection.commit()
    with pytest.raises(EmbeddingModelUnavailable, match="SHA-256"):
        create_embedder(settings, embedding_model_id=2)
    store.connection.execute(
        "UPDATE embedding_models SET file_sha256=? WHERE embedding_model_id=2",
        (hashlib.sha256(path.read_bytes()).hexdigest(),),
    )
    store.connection.commit()
    embedder = create_embedder(settings, embedding_model_id=2)
    assert CHUNKER_VERSION in embedder.model_version
    (path.parent / "tokenizer.json").unlink()
    with pytest.raises(EmbeddingModelUnavailable, match="tokenizer is missing"):
        create_embedder(settings, embedding_model_id=2)
    with pytest.raises(EmbeddingModelUnavailable, match="not registered"):
        create_embedder(settings, embedding_model_id=999)


def test_factory_imported_receipt_and_tokenizer_fallback(
    state: tuple[Settings, NumpyStore, JobContext],
) -> None:
    settings, store, _ = state
    bundled = tiny_bundle(settings.bundled_models_dir / "nomic-embed-text-v1.5")
    imported = tiny_bundle(settings.data_dir / "models" / "v2")
    store.connection.execute(
        "UPDATE embedding_models SET file_sha256=? WHERE embedding_model_id=2",
        (hashlib.sha256(imported.read_bytes()).hexdigest(),),
    )
    store.connection.commit()
    directory = settings.data_dir / "models" / "receipts"
    directory.mkdir()
    (directory / "v2.json").write_text(
        json.dumps({"model_type": "embedding", "model_id": 2, "path": str(imported)})
    )
    first = create_embedder(settings, embedding_model_id=2)
    assert first.model_path == imported
    (imported.parent / "tokenizer.json").unlink()
    fallback = create_embedder(settings, embedding_model_id=2)
    assert fallback.model_path == imported
    np.testing.assert_allclose(first.embed(["Python"]), fallback.embed(["Python"]))
    assert bundled.exists()
    imported.unlink()
    with pytest.raises(EmbeddingModelUnavailable, match="model is missing"):
        create_embedder(settings, embedding_model_id=2)
