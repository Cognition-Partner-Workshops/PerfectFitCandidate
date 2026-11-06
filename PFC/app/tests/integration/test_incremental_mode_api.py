"""Recorded incremental/full provenance through authenticated run routes."""

import json

import pytest
from test_ingestion import data
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import seed_demand, seed_resource

from pfc.db.connection import database


def commit_ct(api: Api, skills: str) -> None:
    client, headers = api
    upload = client.post(
        "/api/v1/uploads/ct",
        headers=headers,
        files={"file": ("ct.xlsx", data("ct", [{"Skills": skills}]))},
    )
    assert upload.status_code == 200, upload.text
    run_id = upload.json()["ingestion_run_id"]
    committed = client.post(f"/api/v1/ingestion-runs/{run_id}/commit", headers=headers)
    assert committed.status_code == 200, committed.text


def test_ct_edit_reports_incremental_mode_and_requested_demand_ids(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_demand(db, grade="B1")
    commit_ct(api, "SQL")
    base_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    with database(client.app.state.settings) as db:
        before = [dict(row) for row in db.execute("SELECT * FROM resources")]
    commit_ct(api, "SQL, Python")
    with database(client.app.state.settings) as db:
        after = [dict(row) for row in db.execute("SELECT * FROM resources")]
        assert len(before) == len(after) == 1
        assert before[0]["resource_id"] == after[0]["resource_id"]
        assert before[0]["skills_raw"] == "SQL"
        assert after[0]["skills_raw"] == "SQL, Python"
        assert after[0]["lifecycle"] == "active"
    body = {"incremental": True, "scope": "full", "demandIds": []}
    run_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json=body))
    response = client.get(f"/api/v1/match-runs/{run_id}")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["mode"] == "incremental"
    assert view["demandIds"] == body["demandIds"]
    with database(client.app.state.settings) as db:
        recorded = json.loads(
            db.execute("SELECT scope_json FROM match_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        )
    assert view["modeReason"] == recorded["reason"] == "inputs_compared"
    assert recorded["base_run_id"] == base_id
    assert recorded["dirty_resources"] == 1


@pytest.mark.parametrize("api", ["Admin"], indirect=True)
def test_occ_synonym_edit_reports_exact_full_fallback_reason(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_demand(db, grade="B1")
        seed_resource(db, grade="B1")
    base_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    synonym = client.post(
        "/api/v1/occ/synonyms",
        headers=headers,
        json={
            "canonical": "SQL",
            "term": "query language alias",
            "relation": "abbreviation",
            "credit": 1,
            "status": "approved",
        },
    )
    assert synonym.status_code == 201, synonym.text
    run_id = finish(
        client, client.post("/api/v1/match-runs", headers=headers, json={"incremental": True})
    )
    response = client.get(f"/api/v1/match-runs/{run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "full"
    assert response.json()["modeReason"] == "config_changed"
    with database(client.app.state.settings) as db:
        rows = [
            db.execute("SELECT * FROM match_runs WHERE run_id=?", (id,)).fetchone()
            for id in (base_id, run_id)
        ]
        assert rows[0]["config_key"] != rows[1]["config_key"]
        assert rows[0]["synonyms_version"] != rows[1]["synonyms_version"]
        assert json.loads(rows[1]["scope_json"])["reason"] == response.json()["modeReason"]


@pytest.mark.parametrize("scope_json", [None, "{}", '{"mode":"incremental","demand_ids":[1', "[]"])
def test_legacy_or_malformed_scope_has_no_mode_or_demand_ids(
    api: Api, scope_json: str | None
) -> None:
    client, headers = api
    run_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    with database(client.app.state.settings) as db:
        db.execute("UPDATE match_runs SET scope_json=? WHERE run_id=?", (scope_json, run_id))
    response = client.get(f"/api/v1/match-runs/{run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["mode"] is None
    assert response.json()["modeReason"] is None
    assert response.json()["demandIds"] == []


def test_incremental_demand_scope_reports_recorded_full_fallback(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, grade="B1")
        seed_resource(db, grade="B1")
    finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    body = {"incremental": True, "scope": "demands", "demandIds": [demand_id]}
    run_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json=body))
    response = client.get(f"/api/v1/match-runs/{run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "full"
    assert response.json()["modeReason"] == "scope_not_full"
    assert response.json()["demandIds"] == body["demandIds"]
    with database(client.app.state.settings) as db:
        recorded = json.loads(
            db.execute("SELECT scope_json FROM match_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        )
    assert recorded["reason"] == response.json()["modeReason"]
