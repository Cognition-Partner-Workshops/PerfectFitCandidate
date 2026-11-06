import hashlib
import json
import sqlite3
from collections.abc import Callable
from threading import Event, Thread

import numpy as np
import pytest

from pfc.adapters.vector.embeddings import FakeEmbedder, Task, Vectors
from pfc.adapters.vector.stores import NumpyStore
from pfc.config import Settings
from pfc.db.connection import connect
from pfc.jobs.runner import JobCancelled, JobContext
from pfc.services.embeddings.chunking import CHUNKER_VERSION
from pfc.services.embeddings.reembed import (
    EmbeddingPipelineMismatch,
    EmbeddingSourceChanged,
    ReembedCounts,
    reembed,
)
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable, create_embedder

from .conftest import NOW, cv, jd, tiny_bundle

State = tuple[Settings, NumpyStore, JobContext]


class TrackingEmbedder(FakeEmbedder):
    def __init__(self, callback: Callable[[], None] | None = None, dimensions: int = 8) -> None:
        super().__init__(dimensions)
        self.calls: list[tuple[list[str], Task]] = []
        self.callback = callback

    def embed(self, texts: list[str], *, task: Task = "document") -> Vectors:
        self.calls.append((texts, task))
        if self.callback:
            self.callback()
        return super().embed(texts, task=task)


def active(store: NumpyStore) -> int:
    return int(
        store.connection.execute(
            "SELECT embedding_model_id FROM embedding_models WHERE is_active=1"
        ).fetchone()[0]
    )


def run(state: State, embedder: FakeEmbedder | None = None) -> ReembedCounts:
    settings, store, context = state
    return reembed(
        settings, context, embedding_model_id=2, embedder=embedder or FakeEmbedder(8), store=store
    )


def test_real_onnx_reembed_and_unchanged_resume(state: State) -> None:
    settings, store, context = state
    db = store.connection
    model = tiny_bundle(settings.bundled_models_dir / "nomic-embed-text-v1.5")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    db.execute("UPDATE embedding_models SET file_sha256=? WHERE embedding_model_id=2", (digest,))
    db.commit()
    jd(db, 1, "Python SQL", "Engineer")
    cv(db, 1, "Python")
    db.execute(
        "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,created_at) "
        "VALUES (1,1,1,'confirmed',?)",
        (NOW,),
    )
    db.execute(
        "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,created_at) "
        "VALUES (2,1,2,'draft',?)",
        (NOW,),
    )
    db.execute(
        "INSERT INTO jd_requirement_items(item_id,jd_req_id,kind,text,position) "
        "VALUES (1,1,'mandatory_skill','Python',0)"
    )
    db.execute(
        "INSERT INTO jd_requirement_items(item_id,jd_req_id,kind,text,position) "
        "VALUES (2,2,'mandatory_skill','Excluded draft',0)"
    )
    db.commit()
    embedder = create_embedder(settings, embedding_model_id=2)
    result = reembed(settings, context, embedding_model_id=2, embedder=embedder, store=store)
    assert result == ReembedCounts(embedded=3)
    rows = db.execute(
        "SELECT chunk_id,owner_type,text,text_sha256,token_count FROM embedding_chunks "
        "WHERE embedding_model_id=2 ORDER BY owner_type"
    ).fetchall()
    assert [row[1] for row in rows] == ["cv", "jd", "jd_item"]
    assert rows[0][2] == "search_document: Python"
    assert rows[1][2].startswith("search_document: Engineer\n")
    assert rows[2][2] == "search_query: Python"
    for id, _, text, digest, count in rows:
        assert digest == hashlib.sha256(text.encode()).hexdigest()
        assert count <= 256
        assert store.get_model_version(id) == embedder.model_version
        assert store.get_vector(id) is not None
    assert (
        CHUNKER_VERSION
        in db.execute(
            "SELECT pipeline_version FROM embedding_models WHERE embedding_model_id=2"
        ).fetchone()[0]
    )
    assert active(store) == 2
    assert store.search(np.ones(384), model_version="old-384")[0].id == 1
    again = reembed(settings, context, embedding_model_id=2, embedder=embedder, store=store)
    assert again == ReembedCounts(skipped=3)


def test_title_edit_only_reembeds_its_owner(state: State) -> None:
    _, store, _ = state
    db = store.connection
    jd(db, 1, "Python", "First")
    jd(db, 2, "SQL", "Second")
    embedder = TrackingEmbedder()
    assert run(state, embedder).embedded == 2
    embedder.calls.clear()
    db.execute("UPDATE demands SET demand_request_name='Changed' WHERE demand_id=1")
    db.commit()
    assert run(state, embedder) == ReembedCounts(embedded=1, skipped=1)
    assert embedder.calls == [(["Changed\nPython"], "document")]


def test_reuses_across_owners_but_not_document_query(state: State) -> None:
    _, store, _ = state
    jd(store.connection, 1)
    jd(store.connection, 2)
    cv(store.connection, 1)
    embedder = TrackingEmbedder()
    assert run(state, embedder) == ReembedCounts(embedded=1, reused=2)
    assert embedder.calls == [(["Python"], "document")]
    vectors = [
        store.get_vector(row[0])
        for row in store.connection.execute(
            "SELECT chunk_id FROM embedding_chunks WHERE embedding_model_id=2"
        )
    ]
    np.testing.assert_allclose(vectors[0], vectors[1])
    np.testing.assert_allclose(vectors[1], vectors[2])


def test_removes_chunks_and_retired_owners_without_other_model_deletion(state: State) -> None:
    _, store, _ = state
    db = store.connection
    cv(
        db,
        1,
        sections=json.dumps(
            {
                "skills": "Python",
                "experience": [
                    {"role": "Engineer", "text": "SQL"},
                    {"role": "Architect", "text": "Python"},
                ],
            }
        ),
    )
    assert run(state).embedded == 3
    db.execute(
        "UPDATE cv_versions SET sections_json=? WHERE cv_version_id=1",
        (json.dumps({"skills": "Python"}),),
    )
    db.commit()
    assert run(state) == ReembedCounts(skipped=1, deleted=2)
    assert store.count() == 2
    db.execute("UPDATE cv_versions SET is_latest=0")
    db.commit()
    assert run(state) == ReembedCounts(deleted=1)
    assert store.count() == 1 and store.get_model_version(1) == "old-384"
    assert (
        db.execute("SELECT count(*) FROM embedding_chunks WHERE embedding_model_id=1").fetchone()[0]
        == 1
    )


def test_cancel_after_owner_commit_resumes_without_duplicates(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, context = state
    jd(store.connection, 1, "Python")
    jd(store.connection, 2, "SQL")
    report = context.report_progress

    def cancel(fraction: float, message: str = "") -> None:
        report(fraction, message)
        if message == "Indexed jd 1":
            context.cancelled.set()

    monkeypatch.setattr(context, "report_progress", cancel)
    with pytest.raises(JobCancelled):
        run(state)
    assert active(store) == 1
    assert store.count() == 2
    context.cancelled.clear()
    monkeypatch.setattr(context, "report_progress", report)
    assert run(state) == ReembedCounts(embedded=1, skipped=1)
    assert active(store) == 2 and store.count() == 3


def test_batches_cancel_before_owner_commit(state: State) -> None:
    _, store, context = state
    jd(store.connection, 1, " ".join(f"word{index}" for index in range(9000)))
    embedder = TrackingEmbedder(callback=context.cancelled.set)
    with pytest.raises(JobCancelled):
        run(state, embedder)
    assert len(embedder.calls[0][0]) == 32
    assert store.count() == 1 and active(store) == 1
    context.cancelled.clear()
    embedder.callback = None
    result = run(state, embedder)
    assert result.embedded > 32 and all(len(texts) <= 32 for texts, _ in embedder.calls)
    assert store.count() == result.embedded + 1


def test_activation_failure_is_atomic_and_retry_uses_checkpoints(state: State) -> None:
    _, store, _ = state
    db = store.connection
    jd(db, 1)
    db.execute(
        "CREATE TRIGGER fail_activation BEFORE UPDATE OF is_active ON embedding_models "
        "WHEN NEW.embedding_model_id=2 AND NEW.is_active=1 "
        "BEGIN SELECT RAISE(ABORT,'activation failed'); END"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="activation failed"):
        run(state)
    assert active(store) == 1 and store.count() == 2
    db.execute("DROP TRIGGER fail_activation")
    db.commit()
    assert run(state) == ReembedCounts(skipped=1)
    assert active(store) == 2 and store.get_vector(1) is not None


def test_atomic_owner_failure_and_resume(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store, _ = state
    cv(store.connection, 1, sections=json.dumps({"skills": "Python", "education": "SQL"}))
    original = store.upsert
    calls = 0

    def fail(id: int, vector: Vectors, model_version: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("disk failed")
        original(id, vector, model_version)

    monkeypatch.setattr(store, "upsert", fail)
    with pytest.raises(RuntimeError, match="disk failed"):
        run(state)
    assert store.count() == 1 and active(store) == 1
    assert (
        store.connection.execute(
            "SELECT count(*) FROM embedding_chunks WHERE embedding_model_id=2"
        ).fetchone()[0]
        == 0
    )
    monkeypatch.setattr(store, "upsert", original)
    assert run(state).embedded == 2


def test_source_change_prevents_stale_activation(state: State) -> None:
    _, store, _ = state
    jd(store.connection, 1)

    def edit() -> None:
        store.connection.execute("UPDATE demands SET demand_request_name='Changed'")
        store.connection.commit()

    with pytest.raises(EmbeddingSourceChanged):
        run(state, TrackingEmbedder(callback=edit))
    assert active(store) == 1 and store.count() == 1
    assert run(state).embedded == 1


def test_fail_fast_dimension_hash_and_pipeline(state: State) -> None:
    settings, store, context = state
    with pytest.raises(ValueError, match="dimensions"):
        run(state, TrackingEmbedder(dimensions=7))
    store.connection.execute(
        "UPDATE embedding_models SET file_sha256='pinned' WHERE embedding_model_id=2"
    )
    store.connection.commit()
    with pytest.raises(EmbeddingModelUnavailable, match="verify"):
        run(state)
    store.connection.execute(
        "UPDATE embedding_models SET file_sha256='' WHERE embedding_model_id=2"
    )
    store.connection.execute(
        "INSERT INTO embedding_chunks VALUES (2,'jd',1,'full',0,'text','hash',1,2,?)", (NOW,)
    )
    store.connection.commit()
    with pytest.raises(EmbeddingPipelineMismatch, match="new embedding model row"):
        reembed(settings, context, embedding_model_id=2, embedder=FakeEmbedder(8), store=store)
    assert active(store) == 1


def test_missing_checkpoint_vector_is_repaired_by_reuse(state: State) -> None:
    _, store, _ = state
    jd(store.connection, 1)
    jd(store.connection, 2)
    assert run(state) == ReembedCounts(embedded=1, reused=1)
    rows = store.connection.execute(
        "SELECT chunk_id FROM embedding_chunks WHERE embedding_model_id=2 ORDER BY chunk_id"
    ).fetchall()
    store.delete(rows[1][0])
    assert run(state) == ReembedCounts(reused=1, skipped=1)
    assert store.count() == 3


def test_empty_sources_activate_and_keep_old_vectors(state: State) -> None:
    assert run(state) == ReembedCounts()
    assert active(state[1]) == 2 and state[1].count() == 1


def test_only_current_confirmed_items_and_latest_cvs_are_loaded(state: State) -> None:
    _, store, _ = state
    db = store.connection
    jd(db, 1)
    cv(db, 1)
    cv(db, 2, "SQL")
    db.execute("UPDATE cv_versions SET is_latest=0 WHERE cv_version_id=1")
    db.execute("INSERT INTO demand_versions VALUES (2,1,2,'old','old','old',NULL,?)", (NOW,))
    db.execute(
        "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,created_at) "
        "VALUES (1,2,1,'confirmed',?)",
        (NOW,),
    )
    db.execute(
        "INSERT INTO jd_requirement_items(item_id,jd_req_id,kind,text,canonical_skill_id,"
        "source_span,position) VALUES (1,1,'mandatory_skill','Old item',NULL,NULL,0)"
    )
    db.commit()
    run(state)
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT owner_type,owner_id FROM embedding_chunks "
            "WHERE embedding_model_id=2 ORDER BY owner_type"
        )
    ] == [("cv", 2), ("jd", 1)]


def test_waiting_job_can_cancel_while_embedding_lock_is_held(state: State) -> None:
    settings, store, context = state
    jd(store.connection, 1)
    entered, release, waiting = Event(), Event(), Event()
    errors: list[BaseException] = []

    def block() -> None:
        entered.set()
        assert release.wait(10)

    def first() -> None:
        try:
            run(state, TrackingEmbedder(callback=block))
        except BaseException as exc:
            errors.append(exc)

    other_db = connect(settings)
    other_store = NumpyStore(other_db, dimensions=8)
    other_context = JobContext(context.runner, 1, Event())
    other_embedder = TrackingEmbedder()

    def second() -> None:
        waiting.set()
        try:
            reembed(
                settings,
                other_context,
                embedding_model_id=2,
                embedder=other_embedder,
                store=other_store,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=first)
    other = Thread(target=second)
    thread.start()
    try:
        assert entered.wait(10)
        other.start()
        assert waiting.wait(10)
        other_context.cancelled.set()
    finally:
        release.set()
        thread.join(10)
        if other.ident is not None:
            other.join(10)
        other_db.close()
    assert not thread.is_alive() and not other.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], JobCancelled)
    assert other_embedder.calls == []
