"""REQ-MAT-07: payload removal, vector invalidation and unchanged-file restoration."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import (
    commit,
    configure_resume_root,
    login,
    resume_docx,
    upload,
    wait_cv_job,
)
from test_matching_service import execute, seed_candidate, seed_cv, seed_demand, seed_resource

from pfc.adapters.vector.embeddings import FakeEmbedder
from pfc.adapters.vector.stores import create_vector_store
from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.services.embeddings.matching_bridge import MatchingVectorStore
from pfc.services.ingestion import cv_sync
from pfc.services.matching.ports import ChunkOwner


def prepare(
    client: TestClient,
    tmp_path: Path,
    cv_text: str = "SQL Python alex@example.com",
) -> tuple[Path, dict[int, int]]:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {},
                {"ShortName": "Taylor", "Email Address": "taylor@example.com"},
            ],
        ),
    )
    resume_docx(root, "Alex.docx", cv_text)
    resume_docx(root, "Taylor.docx", "AWS taylor@example.com")
    client.app.state.embedder = FakeEmbedder(8)
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active) "
            "VALUES ('fake','onnx','',8,'fixed-words-v1',1)"
        )
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        versions = {
            row["candidate_id"]: row["cv_version_id"]
            for row in db.execute("SELECT * FROM cv_versions")
        }
    return root, versions


def snapshot(client: TestClient, tables: tuple[str, ...]) -> dict:
    with database(client.app.state.settings) as db:
        return {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
        }


def seed_summary(db: sqlite3.Connection, kind: str, owner: int, version: int) -> None:
    db.execute(
        "INSERT INTO summaries(owner_kind,owner_id,source_version_id,source_sha256,model,"
        "template_id,template_version,sentences_json,dropped_ungrounded,created_by,created_at) "
        "VALUES (?,?,?,'fixture','fake','summary','1',?,0,1,'2026-01-01')",
        (kind, owner, version, json.dumps([{"text": "SQL", "evidence": "SQL"}])),
    )


def matching_cache(
    client: TestClient, versions: dict[int, int]
) -> tuple[MatchingVectorStore, list]:
    cache = MatchingVectorStore(
        client.app.state.settings,
        SimpleNamespace(dim=8, model_version="cache-test"),
    )
    owners = [ChunkOwner("cv", id, "matching", 0, "hash", "model") for id in versions.values()]
    owners.append(ChunkOwner("jd", 999, "matching", 0, "hash", "model"))
    cache.upsert(owners, [[1.0] + [0.0] * 7 for _ in owners])
    return cache, owners


def test_removal_is_scoped_and_sync_restores_unchanged_file(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root, versions = prepare(client, tmp_path, "alex@example.com\nSkills\nSQL; Python")
    settings = client.app.state.settings
    candidate = min(versions)
    path = f"/api/v1/occ/candidates/{candidate}/cv-extractions"
    cache, owners = matching_cache(client, versions)
    original_files = {
        file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in root.iterdir()
    }
    preserved = snapshot(client, ("resources", "candidate_emails", "resumes"))
    with database(settings) as db:
        seed_summary(db, "candidate", candidate, versions[candidate])
        original = dict(
            db.execute(
                "SELECT * FROM cv_versions WHERE cv_version_id=?",
                (versions[candidate],),
            ).fetchone()
        )
        other = dict(
            db.execute(
                "SELECT * FROM cv_versions WHERE cv_version_id!=?",
                (versions[candidate],),
            ).fetchone()
        )
    listing = client.get(path)
    assert listing.status_code == 200
    assert listing.json() == [
        {
            "cvVersionId": original["cv_version_id"],
            "fileName": "Alex.docx",
            "versionNo": original["version_no"],
            "extractedAt": original["extracted_at"],
            "removed": False,
        }
    ]
    removed = client.delete(path)
    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "versionsCleared": 1,
        "chunksDeleted": 1,
        "vectorsDeleted": 1,
        "matchingVectorsDeleted": 1,
    }
    assert client.get(path).json()[0]["removed"]
    assert cache.get_vectors(owners)[0] is None
    assert all(value is not None for value in cache.get_vectors(owners)[1:])
    with database(settings) as db:
        row = dict(
            db.execute(
                "SELECT * FROM cv_versions WHERE cv_version_id=?", (original["cv_version_id"],)
            ).fetchone()
        )
        assert row["text_norm"] == "" and row["sections_json"] is None
        assert row["facts_json"] == "{}" and row["extraction_removed_at"]
        assert not db.execute(
            "SELECT 1 FROM resource_skills WHERE source='CV' AND cv_version_id=?",
            (original["cv_version_id"],),
        ).fetchone()
        for key in ("text_sha256", "version_no", "is_latest", "resume_id", "extracted_at"):
            assert row[key] == original[key]
        assert (
            dict(
                db.execute(
                    "SELECT * FROM cv_versions WHERE cv_version_id=?", (other["cv_version_id"],)
                ).fetchone()
            )
            == other
        )
        assert (
            db.execute(
                "SELECT count(*) FROM embedding_chunks WHERE owner_id=? AND owner_type='cv'",
                (original["cv_version_id"],),
            ).fetchone()[0]
            == 0
        )
        store = create_vector_store(db, dimensions=8)
        results = store.search(
            client.app.state.embedder.embed(["SQL"])[0],
            model_version=client.app.state.embedder.model_version,
            namespace="cv",
        )
        assert len(results) == 1
        assert not db.execute(
            "SELECT 1 FROM embedding_chunks WHERE owner_id=? AND chunk_id=?",
            (original["cv_version_id"], results[0].id),
        ).fetchone()
    assert snapshot(client, tuple(preserved)) == preserved
    assert {
        file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in root.iterdir()
    } == original_files
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == 0 and summary["embedded"] == 1
    with database(settings) as db:
        row = dict(
            db.execute(
                "SELECT * FROM cv_versions WHERE cv_version_id=?", (original["cv_version_id"],)
            ).fetchone()
        )
        assert row["text_norm"] == original["text_norm"]
        assert row["facts_json"] == original["facts_json"]
        assert row["sections_json"] is None and row["extraction_removed_at"] is None
        assert db.execute("SELECT count(*) FROM cv_versions").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM embedding_chunks").fetchone()[0] == 2
        assert [
            r[0]
            for r in db.execute(
                "SELECT skill_raw FROM resource_skills WHERE cv_version_id=? "
                "ORDER BY resource_skill_id",
                (original["cv_version_id"],),
            )
        ] == ["SQL", "Python"]
    response = client.get(f"/api/v1/candidates/{candidate}/summary")
    assert response.status_code == 404
    assert response.json()["code"] == "SUMMARY_NOT_FOUND"


def test_summary_purge_only_targets_newly_cleared_versions(
    client: TestClient, tmp_path: Path
) -> None:
    _, versions = prepare(client, tmp_path)
    candidate = min(versions)
    other = max(versions)
    settings = client.app.state.settings
    with database(settings) as db:
        removed = db.execute(
            "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
            "extracted_by,is_latest,extracted_at,extraction_removed_at) "
            "SELECT resume_id,candidate_id,2,'removed','',extracted_by,0,extracted_at,"
            "'2026-01-01' FROM cv_versions WHERE cv_version_id=?",
            (versions[candidate],),
        ).lastrowid
        assert removed is not None
        demand = seed_demand(db)
        demand_version = db.execute(
            "SELECT current_version_id FROM demands WHERE demand_id=?", (demand,)
        ).fetchone()[0]
        seed_summary(db, "candidate", candidate, versions[candidate])
        seed_summary(db, "candidate", candidate, removed)
        seed_summary(db, "candidate", other, versions[other])
        seed_summary(db, "demand", demand, demand_version)
        preserved = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM summaries WHERE NOT "
                "(owner_kind='candidate' AND source_version_id=?) ORDER BY summary_id",
                (versions[candidate],),
            )
        ]
    response = client.delete(f"/api/v1/occ/candidates/{candidate}/cv-extractions")
    assert response.status_code == 200, response.text
    assert "summariesCleared" not in response.json()
    with database(settings) as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM summaries ORDER BY summary_id")
        ] == preserved
        event = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE action='CV_EXTRACTIONS_REMOVED' "
                "ORDER BY audit_id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert event["summaries_cleared"] == 1
    assert client.delete(f"/api/v1/occ/candidates/{candidate}/cv-extractions").status_code == 200
    with database(settings) as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM summaries ORDER BY summary_id")
        ] == preserved
        event = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE action='CV_EXTRACTIONS_REMOVED' "
                "ORDER BY audit_id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert event["summaries_cleared"] == 0


@pytest.mark.parametrize("preserved_schema", [False, True])
def test_removed_extraction_restores_structured_facts(
    client: TestClient,
    tmp_path: Path,
    preserved_schema: bool,
) -> None:
    text = (
        "Name: Alex Border\nPhone: +44 123456789\nalex@example.com\nWork Experience\n"
        "Engineer at Example\nEducation\nBSc Computing\nProjects\nAtlas: SQL Python\n"
        "Technical Skills\nSQL, Python"
    )
    _, versions = prepare(client, tmp_path, text)
    candidate = min(versions)
    settings = client.app.state.settings
    with database(settings) as db:
        original = dict(
            db.execute(
                "SELECT * FROM cv_versions WHERE cv_version_id=?",
                (versions[candidate],),
            ).fetchone()
        )
    original_facts = json.loads(original["facts_json"])
    assert original_facts["facts_schema"] == 2
    assert original_facts["facts"]["name"]["value"] == "Alex Border"
    assert original_facts["facts"]["phone"]["value"] == "+44 123456789"
    assert all(
        original_facts["facts"][key]
        for key in (
            "experience",
            "qualifications",
            "projects",
            "skills",
        )
    )
    response = client.delete(f"/api/v1/occ/candidates/{candidate}/cv-extractions")
    assert response.status_code == 200
    with database(settings) as db:
        db.execute(
            "UPDATE cv_versions SET facts_json=?,extracted_by='legacy',prompt_version='stale' "
            "WHERE cv_version_id=?",
            (original["facts_json"] if preserved_schema else "{}", versions[candidate]),
        )
    result = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert result["new_versions"] == 0 and result["embedded"] == 1
    with database(settings) as db:
        restored = dict(
            db.execute(
                "SELECT * FROM cv_versions WHERE cv_version_id=?",
                (versions[candidate],),
            ).fetchone()
        )
        assert restored["text_norm"] == original["text_norm"]
        assert json.loads(restored["facts_json"]) == original_facts
        assert restored["extraction_removed_at"] is None
        assert restored["extracted_by"] == original["extracted_by"]
        assert restored["prompt_version"] == original["prompt_version"]
        for key in ("cv_version_id", "resume_id", "version_no", "text_sha256", "is_latest"):
            assert restored[key] == original[key]
    repeated = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeated["new_versions"] == 0 and repeated["embedded"] == 0


def test_removal_after_facts_lookup_defers_refill(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Name: Alex Border\nalex@example.com\nWork Experience\nSQL Python engineer"
    _, versions = prepare(client, tmp_path, text)
    candidate = min(versions)
    settings = client.app.state.settings
    with database(settings) as db:
        original = db.execute(
            "SELECT v.*,r.file_sha256 FROM cv_versions v JOIN resumes r USING(resume_id) "
            "WHERE v.cv_version_id=?",
            (versions[candidate],),
        ).fetchone()
    removed = False

    @contextmanager
    def remove_after_lookup(sync_settings: Settings) -> Iterator[sqlite3.Connection]:
        nonlocal removed
        queries: list[str] = []
        with database(sync_settings) as db:
            db.set_trace_callback(queries.append)
            yield db
        if not removed and any(
            query.startswith("SELECT v.facts_json,v.extraction_removed_at")
            and original["file_sha256"] in query
            for query in queries
        ):
            removed = True
            response = client.delete(f"/api/v1/occ/candidates/{candidate}/cv-extractions")
            assert response.status_code == 200

    monkeypatch.setattr(cv_sync, "database", remove_after_lookup)
    result = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert removed
    assert result["new_versions"] == 0 and result["embedded"] == 0
    with database(settings) as db:
        deferred = db.execute(
            "SELECT * FROM cv_versions WHERE cv_version_id=?", (versions[candidate],)
        ).fetchone()
        assert deferred["extraction_removed_at"] is not None
        assert deferred["text_norm"] == "" and json.loads(deferred["facts_json"]) == {}
        assert (
            db.execute(
                "SELECT count(*) FROM embedding_chunks WHERE owner_type='cv' AND owner_id=?",
                (versions[candidate],),
            ).fetchone()[0]
            == 0
        )
    retry = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert retry["new_versions"] == 0 and retry["embedded"] == 1
    with database(settings) as db:
        restored = db.execute(
            "SELECT * FROM cv_versions WHERE cv_version_id=?", (versions[candidate],)
        ).fetchone()
        assert restored["extraction_removed_at"] is None
        assert restored["text_norm"] == original["text_norm"]
        assert json.loads(restored["facts_json"]) == json.loads(original["facts_json"])


def test_extraction_filenames_are_masked(client: TestClient, tmp_path: Path) -> None:
    _, versions = prepare(client, tmp_path)
    candidate = min(versions)
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE resumes SET file_name='alex@example.com.docx' WHERE candidate_id=?",
            (candidate,),
        )
    response = client.get(f"/api/v1/occ/candidates/{candidate}/cv-extractions")
    assert response.status_code == 200
    assert "alex@example.com" not in response.text
    assert response.json()[0]["fileName"] == "al**@example.com.docx"
    detail = client.get(f"/api/v1/candidates/{candidate}")
    assert detail.status_code == 200
    assert response.json()[0]["fileName"] == detail.json()["cvVersions"][0]["fileName"]


def test_remove_all_empty_repeat_and_audit(client: TestClient, tmp_path: Path) -> None:
    _, versions = prepare(client, tmp_path)
    cache, owners = matching_cache(client, versions)
    response = client.delete("/api/v1/occ/cv-extractions")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "versionsCleared": 2,
        "chunksDeleted": 2,
        "vectorsDeleted": 2,
        "matchingVectorsDeleted": 2,
    }
    assert cache.get_vectors(owners)[:2] == [None, None]
    assert cache.get_vectors(owners)[2] is not None
    assert client.delete("/api/v1/occ/cv-extractions").json() == {
        "versionsCleared": 0,
        "chunksDeleted": 0,
        "vectorsDeleted": 0,
        "matchingVectorsDeleted": 0,
    }
    with database(client.app.state.settings) as db:
        assert all(
            row[0] == "" and row[1]
            for row in db.execute("SELECT text_norm,extraction_removed_at FROM cv_versions")
        )
        assert db.execute("SELECT count(*) FROM embedding_chunks").fetchone()[0] == 0
        events = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT details_json FROM audit_log WHERE action='CV_EXTRACTIONS_REMOVED'"
            )
        ]
        assert len(events) == 2
        assert events[0]["scope"] == "all" and events[0]["candidate_id"] is None
        assert events[0]["versions_cleared"] == 2
        assert "SQL" not in json.dumps(events) and "@" not in json.dumps(events)
        assert verify_chain(db)


def test_removal_rolls_back_both_caches(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, versions = prepare(client, tmp_path)
    cache, owners = matching_cache(client, versions)
    with database(client.app.state.settings) as db:
        for candidate, version in versions.items():
            seed_summary(db, "candidate", candidate, version)
    before = snapshot(
        client, ("cv_versions", "embedding_chunks", "pfc_vector_records", "summaries")
    )

    def fail(*args, **kwargs) -> None:
        raise RuntimeError("Audit failed")

    monkeypatch.setattr("pfc.services.occ.cv_extractions.append_audit", fail)
    response = client.delete("/api/v1/occ/cv-extractions")
    assert response.status_code == 500
    assert snapshot(client, tuple(before)) == before
    assert all(value is not None for value in cache.get_vectors(owners))


def test_removal_preserves_old_run_and_ct_skills(client: TestClient) -> None:
    settings = client.app.state.settings
    with database(settings) as db:
        seed_demand(db)
        candidate = seed_candidate(db)
        resource = seed_resource(db, "CVPerson", candidate=candidate)
        cv_id = seed_cv(db, resource, candidate)
        db.execute(
            "INSERT INTO resource_skills(resource_id,source,skill_raw,skill_norm) "
            "VALUES (?,'CT','SQL','sql')",
            (resource,),
        )
    execute(settings, client.app.state.job_runner)
    tables = (
        "resources",
        "candidate_emails",
        "resumes",
        "match_runs",
        "match_results",
        "match_cv_scores",
        "score_breakdowns",
    )
    before = snapshot(client, tables)
    result_id = next(
        row["result_id"] for row in before["match_results"] if row["resource_id"] == resource
    )
    detail = client.get(f"/api/v1/match-results/{result_id}")
    assert detail.status_code == 200, detail.text
    response = client.delete(f"/api/v1/occ/candidates/{candidate}/cv-extractions")
    assert response.status_code == 200
    assert snapshot(client, tables) == before
    with database(settings) as db:
        assert [
            tuple(row)
            for row in db.execute("SELECT source,cv_version_id,skill_raw FROM resource_skills")
        ] == [("CT", None, "SQL")]
    assert client.get(f"/api/v1/match-results/{result_id}").json() == detail.json()
    with database(settings) as db:
        assert db.execute(
            "SELECT cv_version_id FROM cv_versions WHERE cv_version_id=?", (cv_id,)
        ).fetchone()


def test_access_errors_and_empty_candidate(client: TestClient) -> None:
    routes = [
        ("GET", "/api/v1/occ/candidates/9999/cv-extractions"),
        ("DELETE", "/api/v1/occ/candidates/9999/cv-extractions"),
        ("DELETE", "/api/v1/occ/cv-extractions"),
    ]
    login(client, "Analyst")
    for method, path in routes:
        assert client.request(method, path).status_code == 403
    login(client)
    del client.headers["X-CSRF-Token"]
    for method, path in routes[1:]:
        assert client.request(method, path).status_code == 403
    login(client)
    for method, path in routes[:2]:
        assert client.request(method, path).status_code == 404
    with database(client.app.state.settings) as db:
        candidate = seed_candidate(db)
    path = f"/api/v1/occ/candidates/{candidate}/cv-extractions"
    assert client.get(path).json() == []
    assert client.delete(path).json()["versionsCleared"] == 0
