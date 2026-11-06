"""Numerical and optional local-artifact checks (REQ-LLM-07)."""

import os
from pathlib import Path

import numpy as np
import pytest

from pfc.adapters.vector.embeddings import FakeEmbedder, OnnxEmbedder, mean_pool, normalise


def test_pool_normalise_truncate():
    hidden = np.array([[[3, 4, 9], [1, 2, 3], [99, 99, 99]]], dtype=np.float32)
    pooled = mean_pool(hidden, np.array([[1, 1, 0]], dtype=np.int64))
    np.testing.assert_array_equal(pooled, [[2, 3, 6]])
    np.testing.assert_allclose(normalise(pooled, 2), [[2 / np.sqrt(13), 3 / np.sqrt(13)]])
    np.testing.assert_array_equal(mean_pool(hidden, np.zeros((1, 3), dtype=np.int64)), [[0, 0, 0]])
    np.testing.assert_array_equal(normalise(np.zeros((1, 3), dtype=np.float32)), [[0, 0, 0]])
    assert normalise(np.empty((0, 3), dtype=np.float32)).shape == (0, 3)
    with pytest.raises(ValueError):
        mean_pool(hidden, np.ones((1, 2), dtype=np.int64))
    for size in (0, 4):
        with pytest.raises(ValueError):
            normalise(pooled, size)


def test_fake_deterministic_prefix_and_empty():
    embedder = FakeEmbedder(32)
    vectors = embedder.embed(["hello", "", "hello"])
    np.testing.assert_array_equal(vectors[0], vectors[2])
    np.testing.assert_array_equal(vectors, FakeEmbedder(32).embed(["hello", "", "hello"]))
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-6)
    assert not np.array_equal(vectors[:1], embedder.embed(["hello"], task="query"))
    assert vectors.dtype == np.float32
    assert embedder.embed([]).shape == (0, 32)
    with pytest.raises(ValueError):
        FakeEmbedder(0)


@pytest.mark.skipif(
    not os.environ.get("PFC_EMBEDDING_MODEL_PATH"), reason="Local ONNX not supplied"
)
def test_local_onnx():
    model = Path(os.environ["PFC_EMBEDDING_MODEL_PATH"])
    tokenizer = Path(
        os.environ.get("PFC_EMBEDDING_TOKENIZER_PATH", str(model.parent / "tokenizer.json"))
    )
    embedder = OnnxEmbedder(model, tokenizer, dimensions=256, batch_size=1)
    vectors = embedder.embed(["example", ""])
    assert vectors.shape == (2, 256)
    assert embedder.embed([]).shape == (0, 256)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    np.testing.assert_allclose(vectors, embedder.embed(["example", ""]), atol=1e-6)
    assert "nomic-embed-text-v1.5:" in embedder.model_version
