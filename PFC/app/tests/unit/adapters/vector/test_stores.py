"""Shared store contract: model isolation and cosine retrieval (REQ-MAT-01, REQ-LLM-07)."""

import sqlite3

import numpy as np
import pytest

from pfc.adapters.vector import stores


@pytest.fixture(params=[stores.NumpyStore, stores.SqliteVecStore])
def store(request):
    try:
        result = request.param(dimensions=3)
    except stores.ExtensionUnavailable:
        pytest.skip("sqlite-vec extension unavailable")
    yield result
    result.close()


def test_contract(store):
    vector = np.array([2, 0, 0], dtype=np.float32)
    assert store.search(vector, model_version="v1") == []
    assert store.count() == 0
    assert store.get_model_version(999) is None
    store.delete(999)
    store.upsert(1, vector, "v1", metadata={"owner_type": "cv"}, namespace="cv")
    store.upsert(2, np.array([1, 1, 0]), "v1", namespace="jd")
    store.upsert(3, vector, "v2", namespace="cv")
    store.upsert(4, -vector, "v1", namespace="cv")
    assert store.count() == 4
    assert store.count(model_version="v1") == 3
    results = store.search(vector, model_version="v1", top_k=20)
    assert [r.id for r in results] == [1, 2, 4]
    assert [r.model_version for r in results] == ["v1"] * 3
    np.testing.assert_allclose([r.score for r in results], [1, 1 / np.sqrt(2), -1], atol=1e-6)
    assert results[0].metadata == {"owner_type": "cv"}
    assert [r.id for r in store.search(vector, model_version="v1", namespace="cv")] == [1, 4]
    assert store.search(vector, model_version="v1", top_k=0) == []
    assert len(store.search(vector, model_version="v1", top_k=1)) == 1
    store.upsert(1, vector, "v2")
    assert store.get_model_version(1) == "v2"
    assert store.count() == 4
    store.delete(1)
    assert store.get_model_version(1) is None
    assert store.count() == 3


def test_ties_and_invalid(store):
    vector = np.ones(3, dtype=np.float32)
    for id in (5, 2):
        store.upsert(id, vector, "v1")
    assert [r.id for r in store.search(vector, model_version="v1")] == [2, 5]
    for bad in (np.zeros(3), np.ones(2), np.array([np.nan, 1, 1])):
        with pytest.raises(ValueError):
            store.upsert(9, bad, "v1")
        with pytest.raises(ValueError):
            store.search(bad, model_version="v1")
    with pytest.raises(ValueError):
        store.upsert(9, vector, "")
    with pytest.raises(ValueError):
        store.search(vector, model_version="")
    with pytest.raises(ValueError):
        store.search(vector, model_version="v1", top_k=-1)
    assert store.count() == 2


@pytest.mark.parametrize("implementation", [stores.NumpyStore, stores.SqliteVecStore])
def test_persistence_and_transaction(tmp_path, implementation):
    path = tmp_path / "vectors.db"
    try:
        store = implementation(path, dimensions=3)
    except stores.ExtensionUnavailable:
        pytest.skip("sqlite-vec extension unavailable")
    store.upsert(1, np.ones(3), "version")
    store.close()
    store = implementation(path, dimensions=3)
    assert store.count() == 1
    assert store.search(np.ones(3), model_version="version")[0].id == 1
    store.close()
    connection = sqlite3.connect(path)
    store = implementation(connection, dimensions=3)
    connection.execute("BEGIN")
    store.upsert(2, np.ones(3), "version")
    connection.rollback()
    assert store.count() == 1
    store.close()
    assert connection.execute("SELECT 1").fetchone() == (1,)
    connection.close()


def test_factory_fallback_and_errors(monkeypatch, tmp_path):
    def unavailable(connection):
        raise stores.ExtensionUnavailable("policy")

    monkeypatch.setattr(stores, "load_extension", unavailable)
    store = stores.create_vector_store(tmp_path / "fallback.db", dimensions=3)
    assert type(store) is stores.NumpyStore
    store.upsert(1, np.ones(3), "v")
    store.close()
    with pytest.raises(ValueError, match="dimensions"):
        stores.create_vector_store(tmp_path / "fallback.db", dimensions=4)


def test_factory_prefers_sqlite():
    store = stores.create_vector_store(dimensions=3)
    try:
        if type(store) is stores.NumpyStore:
            pytest.skip("sqlite-vec extension unavailable")
        assert isinstance(store, stores.SqliteVecStore)
    finally:
        store.close()


def test_atomic_index_failure(store, monkeypatch):
    def fail(id, blob):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(store, "_update_index", fail)
    with pytest.raises(RuntimeError, match="index write failed"):
        store.upsert(1, np.ones(3), "v1")
    assert store.count() == 0
