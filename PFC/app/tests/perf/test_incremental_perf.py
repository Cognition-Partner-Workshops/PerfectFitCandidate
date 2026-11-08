"""REQ-MAT-07: 500×500, 5% dirty inputs, warm vectors on a Linux VM."""

import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_nfr01 import (
    NOW,
    PASSWORD,
    SEED,
    SIZE,
    FakeEmbedder,
    FakeVectorStore,
    prime_vectors,
    provenance,
    seed_population,
    submit_and_wait,
)

from pfc.config import Settings
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.matching import MatchingPorts


@pytest.mark.perf
def test_incremental_five_percent_dirty(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        bundled_models_dir=tmp_path / "bundled-models",
        allowed_hosts=["testserver"],
        llm_enabled=False,
        job_workers=1,
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    app = create_app(settings)
    app.state.clock = lambda: NOW
    embedder = FakeEmbedder()
    app.state.matching_ports = ports = MatchingPorts(embedder, vector_store=FakeVectorStore())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": "perf@example.test",
                "display_name": "Incremental Perf",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 201, response.text
        response = client.post(
            "/api/v1/auth/login",
            json={
                "role": "Admin",
                "email": "perf@example.test",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 200
        csrf = response.json()["csrf_token"]
        with database(settings) as db:
            seed_population(db)
            prime_vectors(db, ports)
        calls = embedder.calls
        base, base_seconds = submit_and_wait(client, {"scope": "full"}, csrf)
        dirty = random.Random(SEED).sample(range(1, SIZE + 1), SIZE // 20)
        dirty_demands = random.Random(SEED + 1).sample(range(1, SIZE + 1), SIZE // 20)
        with database(settings) as db:
            db.executemany(
                "UPDATE resources SET location=CASE location WHEN 'London' THEN 'Manchester' "
                "ELSE 'London' END WHERE resource_id=?",
                [(i,) for i in dirty],
            )
            db.executemany(
                "UPDATE demands SET position_start_date='2027-04-01' WHERE demand_id=?",
                [(i,) for i in dirty_demands],
            )
        incremental, incremental_seconds = submit_and_wait(
            client,
            {"scope": "full", "incremental": True},
            csrf,
        )
        full, full_seconds = submit_and_wait(client, {"scope": "full"}, csrf)
        assert embedder.calls == calls, "Both timed runs must use only cached vectors"
        with database(settings) as db:
            run = db.execute("SELECT * FROM match_runs WHERE run_id=?", (incremental,)).fetchone()
            scope = json.loads(run["scope_json"])
            assert scope["mode"] == "incremental" and scope["base_run_id"] == base
            assert scope["dirty_resources"] == scope["dirty_demands"] == SIZE // 20
            columns = [
                r["name"]
                for r in db.execute("PRAGMA table_info(match_results)")
                if r["name"] not in {"result_id", "run_id"}
            ]
            projection = ",".join(columns)
            for left, right in ((incremental, full), (full, incremental)):
                assert not db.execute(
                    f"SELECT {projection} FROM match_results WHERE run_id=? EXCEPT "
                    f"SELECT {projection} FROM match_results WHERE run_id=?",
                    (left, right),
                ).fetchone()
            assert (
                run["dq_summary_json"]
                == db.execute(
                    "SELECT dq_summary_json FROM match_runs WHERE run_id=?",
                    (full,),
                ).fetchone()[0]
            )
        print(
            "\nINCREMENTAL_RESULT="
            + json.dumps(
                {
                    "label": "Linux VM, not target laptop",
                    "size": SIZE,
                    "seed": SEED,
                    "dirty_resources": len(dirty),
                    "dirty_demands": len(dirty_demands),
                    "base_seconds": base_seconds,
                    "incremental_seconds": incremental_seconds,
                    "full_seconds": full_seconds,
                    "speedup": full_seconds / incremental_seconds,
                    "provenance": provenance(),
                },
                sort_keys=True,
            )
        )
        assert incremental_seconds < full_seconds
