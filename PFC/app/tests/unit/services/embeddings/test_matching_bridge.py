import asyncio
import hashlib
import json
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI, Request

from pfc.adapters.vector.embeddings import Vectors
from pfc.adapters.vector.stores import NumpyStore
from pfc.api.routers.match import available
from pfc.config import Settings
from pfc.jobs.runner import JobContext
from pfc.security.problems import Problem
from pfc.services.embeddings.matching_bridge import (
    MatchingEmbedder,
    MatchingVectorStore,
    resolve_matching_ports,
)
from pfc.services.matching import MatchingPorts, start_run
from pfc.services.matching.ports import ChunkOwner, Task
from pfc.services.matching.semantic import VectorCache, model_identity
from pfc.services.occ.seed import seed_reference_data

from .conftest import NOW, jd, tiny_bundle

State = tuple[Settings, NumpyStore, JobContext]


@pytest.fixture
def matching_state(state: State) -> State:
    seed_reference_data(state[1].connection)
    state[1].connection.commit()
    return state


def activate(state: State) -> None:
    settings, store, _ = state
    model = tiny_bundle(settings.bundled_models_dir / "nomic-embed-text-v1.5")
    db = store.connection
    db.execute("UPDATE embedding_models SET is_active=0")
    db.execute(
        "UPDATE embedding_models SET is_active=1,file_sha256=? WHERE embedding_model_id=2",
        (hashlib.sha256(model.read_bytes()).hexdigest(),),
    )
    db.commit()


def request_for(state: State) -> Request:
    settings, _, ctx = state
    app = FastAPI()
    app.state.settings = settings
    app.state.job_runner = ctx.runner
    app.state.matching_ports = MatchingPorts()
    return Request({"type": "http", "app": app})


def wait(ctx: JobContext, id: int) -> None:
    async def terminal() -> dict[str, Any]:
        async for event in ctx.runner.events(id, poll_interval=0.01):
            if event.startswith("event: done"):
                return json.loads(event.split("data: ", 1)[1])
        raise AssertionError("Missing terminal event")

    async def bounded() -> dict[str, Any]:
        return await asyncio.wait_for(terminal(), timeout=20)

    result = asyncio.run(bounded())
    assert result["status"] == "completed", result


def test_run_uses_active_identity_and_persistent_cache(
    matching_state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = matching_state
    activate(state)
    settings, store, ctx = state
    db = store.connection
    jd(db, 1, "Python SQL")
    db.execute(
        "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,created_at) "
        "VALUES (1,1,1,'confirmed',?)",
        (NOW,),
    )
    db.execute(
        "INSERT INTO jd_requirement_items(item_id,jd_req_id,kind,text,position) "
        "VALUES (1,1,'mandatory_skill','Python',0)"
    )
    db.execute(
        "INSERT INTO resources(short_name,short_name_key,skills_raw,eligibility,"
        "row_hash,created_at,updated_at) "
        "VALUES ('Engineer','engineer','Python','eligible','row',?,?)",
        (NOW, NOW),
    )
    db.commit()
    ports = resolve_matching_ports(settings)
    assert isinstance(ports.embedder, MatchingEmbedder)
    row = db.execute("SELECT * FROM embedding_models WHERE is_active=1").fetchone()
    assert model_identity(ports.embedder) == {
        name: row[name] for name in ("name", "runtime", "file_sha256", "dim", "pipeline_version")
    }
    original = ports.embedder.adapter.embed
    seen: list[tuple[str, Task]] = []

    def track(texts: list[str], *, task: Task = "document") -> Vectors:
        seen.extend((text, task) for text in texts)
        return original(texts, task=task)

    monkeypatch.setattr(ports.embedder.adapter, "embed", track)
    run_id, job_id = start_run(settings, ctx.runner, ports)
    wait(ctx, job_id)
    assert seen == [
        ("Skill: Python\nEvidence: Python", "document"),
        ("Python SQL", "document"),
        ("Python", "query"),
    ]
    fresh = resolve_matching_ports(settings)
    assert isinstance(fresh.embedder, MatchingEmbedder)

    def forbidden(texts: list[str], *, task: Task = "document") -> Vectors:
        raise AssertionError("Second run must embed zero new texts")

    monkeypatch.setattr(fresh.embedder.adapter, "embed", forbidden)
    again, job_id = start_run(settings, ctx.runner, fresh)
    wait(ctx, job_id)
    assert {
        row[0]
        for row in db.execute(
            "SELECT embedding_model_id FROM match_runs WHERE run_id IN (?,?)", (run_id, again)
        )
    } == {2}
    assert db.execute("SELECT count(*) FROM embedding_models").fetchone()[0] == 2
    assert store.get_model_version(1) == "old-384"
    assert isinstance(fresh.vector_store, MatchingVectorStore)
    with fresh.vector_store._store() as cache:
        records = cache.connection.execute(
            "SELECT chunk_id,model_version,namespace FROM pfc_vector_records"
        ).fetchall()
        assert len(records) == 3
        assert all(
            0 < id < 2**63 and version == fresh.embedder.model_version and ns == "matching"
            for id, version, ns in records
        )


def test_tasks_are_isolated_in_memory_and_persistent_cache(state: State) -> None:
    activate(state)
    settings, _, _ = state
    ports = resolve_matching_ports(settings)
    cache = VectorCache(ports)
    document = cache.vectors("jd_item", 1, ["Python"], task="document")
    query = cache.vectors("jd_item", 1, ["Python"], task="query")
    assert not np.allclose(document, query)
    keys = list(cache.local)
    assert keys[0].text_sha256 != keys[1].text_sha256 and keys[0].task != keys[1].task
    again = VectorCache(resolve_matching_ports(settings))
    np.testing.assert_allclose(again.vectors("jd_item", 1, ["Python"]), document, atol=1e-7)
    np.testing.assert_allclose(
        again.vectors("jd_item", 1, ["Python"], task="query"), query, atol=1e-7
    )


def test_cache_id_collision_cannot_touch_real_chunks(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    activate(state)
    settings, app_store, _ = state
    ports = resolve_matching_ports(settings)
    assert isinstance(ports.vector_store, MatchingVectorStore)
    cache = ports.vector_store
    assert cache.get_vectors([]) == []
    cache.upsert([], [])
    owner = ChunkOwner("jd", 1, "matching", 0, "hash", "model")
    other = ChunkOwner("jd", 2, "matching", 0, "hash", "model")
    monkeypatch.setattr("pfc.services.embeddings.matching_bridge._cache_id", lambda key: 1)
    vector = [1.0] + [0.0] * 7
    cache.upsert([owner], [vector])
    assert cache.get_vectors([owner]) == [vector]
    for operation in (lambda: cache.get_vectors([other]), lambda: cache.upsert([other], [vector])):
        with pytest.raises(ValueError, match="collision"):
            operation()
    with pytest.raises(ValueError, match="dimensions"):
        cache.upsert([owner], [[1.0]])
    assert app_store.get_model_version(1) == "old-384"
    assert cache.get_vectors([owner]) == [vector]


def test_lazy_resolution_reloads_versions_and_preserves_injection(state: State) -> None:
    activate(state)
    settings, store, _ = state
    request = request_for(state)
    first = available(request)
    assert available(request) is first
    assert isinstance(first.embedder, MatchingEmbedder)
    tokenizer = settings.bundled_models_dir / "nomic-embed-text-v1.5" / "tokenizer.json"
    data = json.loads(tokenizer.read_text())
    data["model"]["vocab"]["Extra"] = 8
    tokenizer.write_text(json.dumps(data))
    changed = available(request)
    assert isinstance(changed.embedder, MatchingEmbedder)
    assert changed is not first and changed.embedder.model_version != first.embedder.model_version
    db = store.connection
    db.execute("UPDATE embedding_models SET is_active=0")
    db.execute(
        "INSERT INTO embedding_models SELECT 3,'third',runtime,file_sha256,dim,normalised,"
        "pipeline_version,1 FROM embedding_models WHERE embedding_model_id=2"
    )
    db.commit()
    switched = available(request)
    assert isinstance(switched.embedder, MatchingEmbedder)
    assert switched.embedder.embedding_model_id == 3
    tokenizer.unlink()
    with pytest.raises(Problem) as error:
        available(request)
    assert error.value.status == 503 and "embedding model files not found at" in error.value.detail
    injected = MatchingPorts(first.embedder)
    request.app.state.matching_ports = injected
    assert available(request) is injected


def test_no_active_model_is_a_clear_503(state: State) -> None:
    _, store, _ = state
    store.connection.execute("UPDATE embedding_models SET is_active=0")
    store.connection.commit()
    with pytest.raises(Problem) as error:
        available(request_for(state))
    assert error.value.status == 503 and error.value.code == "MATCHING_UNAVAILABLE"
    assert "no active embedding model" in error.value.detail
