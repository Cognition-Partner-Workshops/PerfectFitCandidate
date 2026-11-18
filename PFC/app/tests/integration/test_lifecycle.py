"""Lifecycle history, sticky uploads and Phase-1 preservation contracts."""

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
import test_ingestion_approvals
from fastapi.testclient import TestClient
from test_ingestion_approvals import client as client
from test_ingestion_approvals import decision, ingest, login, queue, row
from test_matching_service import seed_demand, seed_resource

from pfc.config import Settings
from pfc.db.audit import append_audit, verify_chain
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.lifecycle import REASONS, events, record_transition
from pfc.services.occ.seed import seed_reference_data


@pytest.fixture(autouse=True)
def unique_workbooks(monkeypatch):
    original = test_ingestion_approvals.Workbook

    def workbook():
        book = original()
        book.properties.identifier = str(uuid4())
        return book

    monkeypatch.setattr(test_ingestion_approvals, "Workbook", workbook)


def change(client: TestClient, entity: str, outcome: str, **extra: object):
    history = client.get(f"/api/v1/lifecycle/events?entity_type={entity}&entity_id=1").json()
    return client.post(
        f"/api/v1/lifecycle/{entity}/1/{'reopen' if outcome == 'reopen' else 'end'}",
        json={
            **({"outcome": outcome} if outcome != "reopen" else {}),
            "reason_code": "correction" if outcome == "reopen" else "other",
            "comment": "Ask owner@example.com on +44 7700 900123",
            "expected_updated_at": row(client, entity)["updated_at"],
            "expected_event_id": history[-1]["event_id"] if history else 0,
            **extra,
        },
    )


def test_reason_metadata(client: TestClient) -> None:
    response = client.get("/api/v1/lifecycle/reasons")
    assert response.status_code == 200
    assert response.json() == {outcome: sorted(codes) for outcome, codes in REASONS.items()}


@pytest.mark.parametrize(
    "kind,entity,outcome", [("dl", "demand", "filled"), ("ct", "resource", "released")]
)
def test_scripted_lifecycle_invariant(client, kind, entity, outcome):
    assert client.get("/api/v1/lifecycle/events").json() == []
    ingest(client, kind, [{}])
    decision(
        client,
        queue(client)[0],
        "modify",
        comment="Reviewed",
        values={"accountName" if kind == "dl" else "location": "Admin override"},
    )
    login(client, "Analyst")
    response = change(client, entity, outcome)
    assert response.status_code == 200, response.text
    ended = row(client, entity)
    login(client)
    count = len(queue(client))
    ingest(client, kind, [{}])
    assert row(client, entity)["lifecycle"] == ended["lifecycle"]
    assert row(client, entity)["account_name" if kind == "dl" else "location"] == "Admin override"
    assert len(queue(client)) == count
    assert change(client, entity, "reopen").status_code == 200
    other = (
        {"Code Requisition": "T21234"}
        if kind == "dl"
        else {"ShortName": "Other", "Email Address": "other@example.com"}
    )
    ingest(client, kind, [other])
    assert row(client, entity)["end_source"] == "upload"
    ingest(client, kind, [{}])
    assert row(client, entity)["end_source"] is None
    assert change(client, entity, outcome).status_code == 200
    field = "Account Name" if kind == "dl" else "Location"
    ingest(client, kind, [{field: "Changed"}])
    assert row(client, entity)["lifecycle"] == ("open" if kind == "dl" else "active")
    pending = next(e for e in queue(client) if e["state"] == "Pending" and e["targetId"] == 1)
    decision(client, pending, "revert")
    with database(client.app.state.settings) as db:
        for table, name in (("demands", "demand"), ("resources", "resource")):
            for item in db.execute(f"SELECT * FROM {table}"):
                latest = db.execute(
                    "SELECT to_state FROM lifecycle_events WHERE entity_type=? AND entity_id=? "
                    "ORDER BY event_id DESC LIMIT 1",
                    (name, item[f"{name}_id"]),
                ).fetchone()
                assert latest[0] == item["lifecycle"]
        assert verify_chain(db)
        assert "owner@example.com" not in str(events(db))
        assert "7700" not in str(events(db))
        audit = db.execute("SELECT details_json FROM audit_log WHERE action='LIFECYCLE_CHANGE'")
        assert all("comment" not in item[0] for item in audit)
    history = client.get(f"/api/v1/lifecycle/events?entity_type={entity}&entity_id=1&limit=1")
    assert history.status_code == 200
    first = history.json()[0]
    rest = client.get(f"/api/v1/lifecycle/events?cursor={first['event_id']}").json()
    assert all(event["event_id"] > first["event_id"] for event in rest)


def test_manual_errors_and_history_immutability(client):
    ingest(client, "dl", [{}])
    assert change(client, "demand", "filled", reason_code="unknown").status_code == 422
    assert change(client, "demand", "filled", comment=" ").status_code == 422
    assert change(client, "demand", "filled", comment="a" * 501).status_code == 422
    assert change(client, "demand", "filled", expected_updated_at="old").status_code == 409
    assert change(client, "demand", "filled", expected_event_id=0).status_code == 409
    assert change(client, "demand", "reopen").status_code == 409
    assert client.get("/api/v1/lifecycle/events?limit=0").status_code == 422
    with database(client.app.state.settings) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE lifecycle_events SET source='manual'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM lifecycle_events")
        db.execute("UPDATE lifecycle_events SET comment=NULL")
    client.cookies.clear()
    assert client.get("/api/v1/lifecycle/events").status_code == 401


def test_archived_upload_restore(client):
    ingest(client, "dl", [{}])
    with database(client.app.state.settings) as db:
        record_transition(db, "demand", 1, "open", "archived", "occ_approval", now="2026-01-01")
    ingest(client, "dl", [{}])
    assert row(client, "demand")["lifecycle"] == "archived"
    ingest(client, "dl", [{"Account Name": "New"}])
    assert row(client, "demand")["lifecycle"] == "open"
    with database(client.app.state.settings) as db:
        assert events(db)[-1]["source"] == "restore"


def test_p2_ex_06_upgrade_preserves_phase1(tmp_path: Path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in Path("pfc/db/migrations").glob("*.sql"):
        if path.name[:4] <= "0012":
            shutil.copyfile(path, directory / path.name)
    settings = Settings(data_dir=tmp_path / "data")
    migrate(settings, directory)
    with database(settings) as db:
        seed_reference_data(db)
        db.execute(
            "INSERT INTO users(user_id,email,display_name,role_id,password_hash,created_at,"
            "updated_at) VALUES (1,'test@example.com','Test',1,'unused','old','old')"
        )
        seed_demand(db)
        seed_resource(db)
        db.execute("INSERT INTO embedding_models VALUES (1,'test','onnx','hash',2,1,'v1',1)")
        template = db.execute("SELECT template_id FROM scoring_templates LIMIT 1").fetchone()[0]
        db.execute(
            "INSERT INTO match_runs(run_id,scope,template_id,embedding_model_id,engine_version,"
            "synonyms_version,config_hash,status) "
            "VALUES (1,'full',?,1,'v1','v1','hash','completed')",
            (template,),
        )
        db.execute(
            "INSERT INTO match_results(result_id,run_id,demand_id,demand_version_id,jd_req_id,"
            "resource_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
            "matching_evidence,gaps_checks,eligibility_evidence) "
            "SELECT 1,1,1,1,jd_req_id,1,75,'Potential',1,1,'exact','Aligned','SQL','','' "
            "FROM jd_requirements LIMIT 1"
        )
        email_template = db.execute(
            "SELECT email_template_id FROM email_templates LIMIT 1"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO email_messages(demand_id,resource_id,result_id,email_template_id,"
            "to_address,cc_json,subject,body_html,status,idempotency_key,created_by,created_at,"
            "updated_at) VALUES "
            "(1,1,1,?,'test@example.com','[]','Role','Body','Draft','key',1,'old','old')",
            (email_template,),
        )
        append_audit(db, ts="old", action="PHASE1")
        before = {
            name: [dict(r) for r in db.execute(f"SELECT * FROM {name}")]
            for name in (
                "demands",
                "resources",
                "demand_versions",
                "jd_requirements",
                "match_runs",
                "match_results",
                "email_messages",
                "audit_log",
            )
        }
    assert migrate(settings) == sorted(
        path.name[:4] for path in Path("pfc/db/migrations").glob("*.sql") if path.name[:4] > "0012"
    )
    with database(settings) as db:
        for name, rows in before.items():
            actual = [dict(r) for r in db.execute(f"SELECT * FROM {name}")]
            assert [{k: r[k] for k in rows[0]} for r in actual] == rows
        assert verify_chain(db)
        assert [(e["entity_type"], e["to_state"], e["source"]) for e in events(db)] == [
            ("demand", "open", "baseline"),
            ("resource", "active", "baseline"),
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
