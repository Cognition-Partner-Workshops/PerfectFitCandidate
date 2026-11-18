"""Archive selection, atomic drift protection, retention and maintenance contracts."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from test_ingestion_approvals import client as client
from test_ingestion_approvals import login
from test_matching_service import seed_candidate, seed_cv, seed_demand, seed_resource

from pfc.db.audit import append_audit, verify_chain
from pfc.db.connection import database
from pfc.jobs.runner import JobRunner
from pfc.security.problems import Problem
from pfc.services import archival, housekeeping
from pfc.services.lifecycle import record_transition

NOW = datetime(2026, 11, 18, tzinfo=UTC)


def seed(client):
    settings = client.app.state.settings
    with database(settings) as db:
        for i in range(3):
            demand = seed_demand(db, f"T{i}", confirmed=False)
            record_transition(db, "demand", demand, "open", "closed", "manual", now=NOW.isoformat())
        seed_resource(db, email="private@example.test", status="Selected")
    return settings


def execute(settings, batch):
    with database(settings) as db:
        db.execute(
            "UPDATE archive_batches SET status='running' WHERE batch_id=?", (batch["batch_id"],)
        )
    return archival.execute_batch(settings, batch["batch_id"], 1, NOW)


@pytest.mark.parametrize(
    "entity,outcome,source,reason",
    [
        ("demand", "filled", "manual", "filled_manual"),
        ("demand", "cancelled", "manual", "cancelled_manual"),
        ("demand", "closed", "manual", "closed_manual"),
        ("demand", "not_in_latest_dl", "upload", "not_in_latest_dl"),
        ("resource", "not_in_latest_ct", "upload", "not_in_latest_ct"),
        ("resource", "off_bench", "manual", "off_bench_manual"),
        ("resource", "released", "manual", "released_manual"),
        ("demand", "reverted", "occ", "closed_occ"),
        ("demand", None, None, "closed_legacy"),
        ("resource", None, None, "off_bench_legacy"),
    ],
)
def test_preview_preserves_end_reason(client, entity, outcome, source, reason):
    settings = client.app.state.settings
    with database(settings) as db:
        entity_id = (
            seed_demand(db, "T19001", confirmed=False) if entity == "demand" else seed_resource(db)
        )
        record_transition(
            db,
            entity,
            entity_id,
            "open" if entity == "demand" else "active",
            "closed" if entity == "demand" else "off_bench",
            "manual",
            now=NOW.isoformat(),
            metadata={"end_outcome": outcome, "end_source": source, "ended_at": NOW.isoformat()},
        )
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
    assert len(batch["items"]) == 1
    assert batch["items"][0]["reason"] == reason


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"status": "Selected"}, "selected"),
        ({"group": "Not on Bench"}, "not_on_bench"),
        ({"end": "2026-10-31", "end_status": "valid"}, "bench_end_set"),
    ],
)
def test_preview_preserves_resource_eligibility_reason(client, kwargs, reason):
    settings = client.app.state.settings
    with database(settings) as db:
        seed_resource(db, **kwargs)
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
    assert len(batch["items"]) == 1
    assert batch["items"][0]["reason"] == reason


def test_exact_selection_and_masked_search(client):
    settings = seed(client)
    response = client.post("/api/v1/occ/archive/preview", json={})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert len(batch["items"]) == 4
    selected = client.put(
        f"/api/v1/occ/archive/batches/{batch['batch_id']}/items",
        json={"items": [{"entity_type": "demand", "entity_id": 2, "included": False}]},
    )
    assert selected.status_code == 200, selected.text
    batch = selected.json()
    assert batch["preview_hash"] != response.json()["preview_hash"]
    expected = {(i["entity_type"], i["entity_id"]) for i in batch["items"] if i["included"]}
    execute(settings, batch)
    with database(settings) as db:
        actual = {
            (e, r[0])
            for e in ("demand", "resource")
            for r in db.execute(f"SELECT {e}_id FROM {e}s WHERE lifecycle='archived'")
        }
        assert actual == expected
        assert verify_chain(db)
    assert client.get("/api/v1/demands").json()["total"] == 0
    assert client.get("/api/v1/resources").json()["total"] == 0
    assert client.get("/api/v1/archive/demands?q=T0").json()["total"] == 1
    archived = client.get("/api/v1/archive/resources")
    assert archived.json()["total"] == 1
    assert "private@example.test" not in archived.text
    csv = client.get("/api/v1/archive/resources/export.csv")
    assert csv.status_code == 200 and "private@example.test" not in csv.text
    assert "pr*****@example.test" in csv.text
    assert client.get("/api/v1/resources/1").status_code == 200
    login(client, "Analyst")
    assert client.get("/api/v1/archive/resources").status_code == 200
    assert client.post("/api/v1/occ/archive/preview", json={}).status_code == 403


def test_drift_rolls_back_entire_batch_and_empty_selection(client):
    settings = seed(client)
    with database(settings) as db:
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
        db.execute("UPDATE demands SET account_name='changed' WHERE demand_id=3")
    with pytest.raises(Problem, match="Data changed"):
        execute(settings, batch)
    with database(settings) as db:
        assert (
            db.execute("SELECT count(*) FROM demands WHERE lifecycle='archived'").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT count(*) FROM resources WHERE lifecycle='archived'").fetchone()[0]
            == 0
        )
        assert archival.batch(db, batch["batch_id"])["status"] == "failed"
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
        batch = archival.select_items(
            db, batch["batch_id"], [{**i, "included": False} for i in batch["items"]]
        )
    response = client.post(
        f"/api/v1/occ/archive/batches/{batch['batch_id']}/run",
        json={"preview_hash": batch["preview_hash"]},
    )
    assert response.status_code == 422


def test_retention_boundary_dependants_and_audit_history(client):
    settings = seed(client)
    with database(settings) as db:
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
    execute(settings, batch)
    with database(settings) as db:
        db.execute(
            "UPDATE demands SET archived_at=? WHERE demand_id=1",
            ((NOW - timedelta(days=400)).isoformat(),),
        )
        db.execute(
            "UPDATE demands SET archived_at=? WHERE demand_id=2",
            ((NOW - timedelta(days=330)).isoformat(),),
        )
        append_audit(db, ts=NOW.isoformat(), user_id=1, action="TEST", details={})
        before = [tuple(r) for r in db.execute("SELECT * FROM audit_log")]
        batch = archival.preview(db, kind="purge", user_id=1, now=NOW)
        assert [(i["entity_type"], i["entity_id"]) for i in batch["items"]] == [("demand", 1)]
    execute(settings, batch)
    with database(settings) as db:
        assert db.execute("SELECT 1 FROM demands WHERE demand_id=1").fetchone() is None
        assert db.execute("SELECT 1 FROM demand_versions WHERE demand_id=1").fetchone() is None
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert [
            tuple(r)
            for r in db.execute("SELECT * FROM audit_log ORDER BY audit_id LIMIT ?", (len(before),))
        ] == before
        assert verify_chain(db)
        history = db.execute(
            "SELECT to_state,comment FROM lifecycle_events "
            "WHERE entity_type='demand' AND entity_id=1 ORDER BY event_id"
        ).fetchall()
        assert history[-1][0] == "purged" and all(r[1] is None for r in history)


def test_housekeeping_files_vacuum_and_backup_rotation(client):
    settings = client.app.state.settings
    root = settings.data_dir
    for name in (
        "tmp/stale",
        "tmp/new",
        "logs/old.log",
        "logs/pfc.log",
        "backups/pre-migration.db",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test")
        old = (NOW - timedelta(days=100)).timestamp()
        os.utime(path, (old, old))
    os.utime(root / "tmp/new", (NOW.timestamp(), NOW.timestamp()))
    with database(settings) as db:
        db.execute("UPDATE settings SET value_json='1' WHERE key='housekeeping.backups_keep'")
    preview = housekeeping.preview(settings, NOW)
    assert preview["temp"]["count"] == preview["logs"]["count"] == 1
    for offset in range(2):
        result = housekeeping.run(
            settings,
            {
                "purge_temp": True,
                "purge_logs": True,
                "purge_orphan_vectors": True,
                "vacuum": True,
                "backup": True,
            },
            1,
            NOW + timedelta(seconds=offset),
        )
        assert result["backup"] == result["vacuum"] == 1
    assert not (root / "tmp/stale").exists() and not (root / "logs/old.log").exists()
    assert (root / "logs/pfc.log").exists() and (root / "tmp/new").exists()
    assert len(list((root / "backups").glob("*-housekeeping.db"))) == 1
    assert (root / "backups/pre-migration.db").exists()


@pytest.mark.parametrize(
    "key,valid,invalid",
    [
        ("archive.retention_months", 24, 0),
        ("housekeeping.temp_days", 14, -1),
        ("housekeeping.log_days", 90, 3651),
        ("housekeeping.backups_keep", 5, True),
    ],
)
def test_settings_ranges_and_audit(client, key, valid, invalid):
    assert client.put("/api/v1/occ/settings", json={key: invalid}).status_code == 422
    response = client.put("/api/v1/occ/settings", json={key: valid})
    assert response.status_code == 200, response.text
    assert response.json()[key] == valid
    with database(client.app.state.settings) as db:
        assert db.execute(
            "SELECT 1 FROM audit_log WHERE action='SETTING_CHANGE' AND entity_id=?", (key,)
        ).fetchone()
        assert verify_chain(db)


@pytest.mark.parametrize("kind", ["archive", "purge"])
def test_archive_job_and_expired_preview(client, kind):
    settings = seed(client)
    if kind == "purge":
        with database(settings) as db:
            archived = archival.preview(db, kind="archive", user_id=1, now=NOW)
        execute(settings, archived)
        with database(settings) as db:
            for table in ("demands", "resources"):
                db.execute(
                    f"UPDATE {table} SET archived_at=?", ((NOW - timedelta(days=400)).isoformat(),)
                )
    with database(settings) as db:
        old = archival.preview(db, kind=kind, user_id=1, now=NOW)
        batch = archival.preview(db, kind=kind, user_id=1, now=NOW)
        assert archival.batch(db, old["batch_id"])["status"] == "expired"
    runner = JobRunner(settings)
    try:
        job_id = archival.submit(
            settings, runner, batch["batch_id"], batch["preview_hash"], 1, NOW, kind=kind
        )
        runner._executor.shutdown(wait=True)
    finally:
        runner.shutdown()
    job = runner.get(job_id)
    assert job.kind == ("archive" if kind == "archive" else "archive_purge")
    assert job.status == "completed"
    assert '"demand": 3' in job.message


@pytest.mark.parametrize("kind", ["archive", "purge"])
def test_cancel_during_atomic_batch_finishes_with_committed_status(client, monkeypatch, kind):
    settings = seed(client)
    if kind == "purge":
        with database(settings) as db:
            archive = archival.preview(db, kind="archive", user_id=1, now=NOW)
        execute(settings, archive)
        with database(settings) as db:
            for table in ("demands", "resources"):
                db.execute(f"UPDATE {table} SET archived_at='2024-01-01'")
    with database(settings) as db:
        preview = archival.preview(db, kind=kind, user_id=1, now=NOW)
    entered, release = threading.Event(), threading.Event()
    original = archival.execute_batch

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(archival, "execute_batch", blocked)
    runner = JobRunner(settings)
    try:
        job_id = archival.submit(
            settings, runner, preview["batch_id"], preview["preview_hash"], 1, NOW, kind=kind
        )
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            cancellation = executor.submit(runner.cancel, job_id)
            try:
                with pytest.raises(TimeoutError):
                    cancellation.result(timeout=0.05)
            finally:
                release.set()
            assert cancellation.result(timeout=5).status == "completed"
        runner._executor.shutdown(wait=True)
        assert runner.get(job_id).status == "completed"
        with database(settings) as db:
            assert archival.batch(db, preview["batch_id"])["status"] == "completed"
            assert db.execute("SELECT count(*) FROM demands").fetchone()[0] == (
                0 if kind == "purge" else 3
            )
    finally:
        release.set()
        runner.shutdown()


def test_housekeeping_job_result_message(client):
    response = client.post("/api/v1/occ/housekeeping", json={"backup": True})
    assert response.status_code == 202
    runner = client.app.state.job_runner
    runner._executor.shutdown(wait=True)
    job = runner.get(response.json()["job_id"])
    assert job.kind == "housekeeping" and job.status == "completed"
    assert json.loads(job.message) == {"purge_temp": 0, "purge_logs": 0, "backup": 1}


def test_housekeeping_orphans_retains_archived_embeddings(client):
    settings = seed(client)
    with database(settings) as db:
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
    execute(settings, batch)
    with database(settings) as db:
        model = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,normalised,"
            "pipeline_version,is_active) VALUES ('test','onnx','hash',2,1,'v1',0)"
        ).lastrowid
        for owner in (1, 999):
            db.execute(
                "INSERT INTO embedding_chunks(owner_type,owner_id,embedding_model_id,text,"
                "text_sha256,section,chunk_no,token_count,created_at) "
                "VALUES ('ct_skills',?,?,'text',?,'skills',1,1,'2026-01-01')",
                (owner, model, str(owner)),
            )
        db.execute("CREATE TABLE pfc_vector_records(chunk_id INTEGER PRIMARY KEY, vector BLOB)")
        db.execute("INSERT INTO pfc_vector_records VALUES (999, X'0000')")
    preview = housekeeping.preview(settings, NOW)
    assert preview["orphan_chunks"] == preview["orphan_vectors"] == 1
    housekeeping.run(settings, {"purge_orphan_vectors": True}, 1, NOW)
    with database(settings) as db:
        assert [r[0] for r in db.execute("SELECT owner_id FROM embedding_chunks")] == [1]
        assert not db.execute("SELECT * FROM pfc_vector_records").fetchall()
        assert (
            db.execute("SELECT count(*) FROM fts_chunks WHERE fts_chunks MATCH 'text'").fetchone()[
                0
            ]
            == 1
        )


def test_resource_purge_deletes_identity_cv_graph(client):
    settings = client.app.state.settings
    with database(settings) as db:
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate, status="Selected")
        seed_cv(db, resource, candidate)
        batch = archival.preview(db, kind="archive", user_id=1, now=NOW)
    execute(settings, batch)
    with database(settings) as db:
        db.execute("UPDATE resources SET archived_at='2024-01-01' WHERE resource_id=?", (resource,))
        batch = archival.preview(db, kind="purge", user_id=1, now=NOW)
    execute(settings, batch)
    with database(settings) as db:
        for table in ("resources", "candidates", "resumes", "cv_versions", "resource_skills"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
