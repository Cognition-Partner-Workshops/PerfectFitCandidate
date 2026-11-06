import asyncio
import json
import logging
import sqlite3
from threading import Event, Thread

import pytest

from pfc.adapters.vector.embeddings import FakeEmbedder, Task, Vectors
from pfc.adapters.vector.stores import NumpyStore, SqliteVecStore
from pfc.config import Settings
from pfc.db.connection import connect
from pfc.jobs.runner import JobCancelled, JobContext
from pfc.services.embeddings import incremental
from pfc.services.embeddings.incremental import submit_incremental_embed
from pfc.services.embeddings.reembed import EmbeddingPipelineMismatch, embed_owners
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable

from .conftest import NOW, cv, jd, tiny_bundle

State = tuple[Settings, NumpyStore, JobContext]


@pytest.mark.parametrize("state", [SqliteVecStore], indirect=True)
def test_incremental_store_open_serializes_competing_writer(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, ctx = state
    db = store.connection
    db.execute("UPDATE embedding_models SET is_active=(embedding_model_id=2)")
    db.commit()
    jd(db, 1)
    ready, release, committed = Event(), Event(), Event()
    errors: list[BaseException] = []

    def writer() -> None:
        other = connect(settings)
        try:
            other.execute("UPDATE jobs SET progress=0.1 WHERE job_id=1")
            ready.set()
            assert release.wait(10)
            other.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            other.close()
            committed.set()

    thread = Thread(target=writer)
    connections: list[sqlite3.Connection] = []

    def connection(settings: Settings) -> sqlite3.Connection:
        result = connect(settings)
        connections.append(result)

        def trace(sql: str) -> None:
            if sql == "BEGIN IMMEDIATE" or sql.startswith("DELETE FROM vec_chunks_d"):
                release.set()
                committed.wait(10)

        result.set_trace_callback(trace)
        thread.start()
        assert ready.wait(10)
        return result

    embedder = FakeEmbedder(8)
    original_embed = embedder.embed

    def embed(texts: list[str], *, task: Task = "document") -> Vectors:
        assert not connections[0].in_transaction
        return original_embed(texts, task=task)

    monkeypatch.setattr(embedder, "embed", embed)
    factory = incremental.create_vector_store

    def open_store(connection: sqlite3.Connection, *, dimensions: int) -> NumpyStore:
        try:
            return factory(connection, dimensions=dimensions)
        except sqlite3.OperationalError as exc:
            print(f"{exc.sqlite_errorcode} {exc.sqlite_errorname}: {exc}")
            raise

    monkeypatch.setattr(incremental, "create_vector_store", open_store)
    monkeypatch.setattr(incremental, "create_embedder", lambda *a, **kw: embedder)
    monkeypatch.setattr(incremental, "connect", connection)
    try:
        job_id = submit_incremental_embed(ctx.runner, settings, [("jd", 1)])
        assert job_id is not None

        async def terminal() -> dict:
            async for event in ctx.runner.events(job_id, poll_interval=0.01):
                if event.startswith("event: done"):
                    return json.loads(event.split("data: ", 1)[1])
            raise AssertionError("Missing job")

        result = asyncio.run(asyncio.wait_for(terminal(), timeout=20))
        assert result["status"] == "completed", result
        assert "embedded=1" in result["message"]
        assert not errors
        assert (
            db.execute(
                "SELECT count(*) FROM embedding_chunks c JOIN pfc_vector_records v USING(chunk_id) "
                "WHERE c.owner_type='jd' AND c.owner_id=1"
            ).fetchone()[0]
            == 1
        )
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(10)


def test_scoped_cleanup_no_scan_or_activation(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, ctx = state
    cv(store.connection, 1)
    jd(store.connection, 1)

    def forbidden(*args: object) -> None:
        raise AssertionError("Scoped embedding must not scan global sources")

    monkeypatch.setattr("pfc.services.embeddings.reembed.load_owners", forbidden)
    args = dict(embedding_model_id=2, embedder=FakeEmbedder(8), store=store)
    counts = embed_owners(settings, ctx, owners=iter([("cv", 1), ("cv", 1)]), **args)
    assert counts.embedded == 1
    store.connection.execute("UPDATE cv_versions SET is_latest=0")
    store.connection.commit()
    counts = embed_owners(settings, ctx, owners=[("cv", 1)], **args)
    assert counts.deleted == 1 and counts.embedded == 0
    assert (
        store.connection.execute(
            "SELECT embedding_model_id FROM embedding_models WHERE is_active=1"
        ).fetchone()[0]
        == 1
    )
    assert store.count() == 1


def test_scoped_pipeline_and_fail_fast(state: State) -> None:
    settings, store, ctx = state
    jd(store.connection, 1)
    kwargs = dict(owners=[("jd", 1)], embedding_model_id=2, store=store)
    with pytest.raises(ValueError, match="dimensions"):
        embed_owners(settings, ctx, embedder=FakeEmbedder(7), **kwargs)
    embed_owners(settings, ctx, embedder=FakeEmbedder(8), **kwargs)
    store.connection.execute(
        "UPDATE embedding_models SET pipeline_version='legacy' WHERE embedding_model_id=2"
    )
    store.connection.commit()
    with pytest.raises(EmbeddingPipelineMismatch, match="run a full re-embed"):
        embed_owners(settings, ctx, embedder=FakeEmbedder(8), **kwargs)
    store.connection.execute(
        "UPDATE embedding_models SET pipeline_version='' WHERE embedding_model_id=2"
    )
    store.connection.commit()
    assert embed_owners(settings, ctx, embedder=FakeEmbedder(8), **kwargs).skipped == 1
    ctx.cancelled.set()
    with pytest.raises(JobCancelled):
        embed_owners(
            settings, ctx, owners=[], embedding_model_id=2, embedder=FakeEmbedder(8), store=store
        )


def test_scoped_empty_and_hash_validation(state: State) -> None:
    settings, store, ctx = state
    embedder = FakeEmbedder(8)
    assert (
        embed_owners(
            settings, ctx, owners=[], embedding_model_id=2, embedder=embedder, store=store
        ).embedded
        == 0
    )
    path = settings.data_dir / "model.onnx"
    path.write_bytes(b"wrong model")
    embedder.model_path = path
    store.connection.execute(
        "UPDATE embedding_models SET file_sha256=? WHERE embedding_model_id=2", ("0" * 64,)
    )
    store.connection.commit()
    with pytest.raises(EmbeddingModelUnavailable, match="SHA-256"):
        embed_owners(
            settings, ctx, owners=[("jd", 1)], embedding_model_id=2, embedder=embedder, store=store
        )


@pytest.mark.parametrize("mode", ["success", "missing", "pipeline"])
def test_incremental_job_lifecycle(
    state: State, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings, store, ctx = state
    runner, db = ctx.runner, store.connection
    assert submit_incremental_embed(runner, settings, iter([])) is None
    db.execute("UPDATE embedding_models SET is_active=0")
    db.commit()
    assert submit_incremental_embed(runner, settings, [("jd", 1)]) is None
    db.execute("UPDATE embedding_models SET is_active=1 WHERE embedding_model_id=2")
    db.commit()
    jd(db, 1)
    if mode != "missing":
        tiny_bundle(settings.bundled_models_dir / "nomic-embed-text-v1.5")
    if mode == "pipeline":
        db.execute(
            "INSERT INTO embedding_chunks VALUES (2,'jd',1,'full',0,'old','hash',1,2,?)", (NOW,)
        )
        db.commit()
    id = submit_incremental_embed(runner, settings, iter([("jd", 1)]))
    assert id is not None

    async def terminal() -> dict:
        async for event in runner.events(id, poll_interval=0.01):
            if event.startswith("event: done"):
                return json.loads(event.split("data: ", 1)[1])
        raise AssertionError("Missing job")

    async def bounded() -> dict:
        return await asyncio.wait_for(terminal(), timeout=20)

    result = asyncio.run(bounded())
    assert result["status"] == ("completed" if mode == "success" else "failed")
    if mode == "success":
        assert "embedded=1" in result["message"]
    elif mode == "pipeline":
        assert "run a full re-embed" in result["message"]
    else:
        assert "model is missing" in result["message"]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
