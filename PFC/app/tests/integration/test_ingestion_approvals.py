"""REQ-ING-03, REQ-ROLE-03, REQ-IN-23..25: committed queues and human identity decisions."""

import json
import sqlite3
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.ingestion import service
from pfc.services.ingestion.workbooks import CT, DL

PASSWORD = "Strong!Random4567"


def login(client: TestClient, role: str = "Admin") -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"role": role, "email": "owner@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as result:
        response = result.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": "owner@example.com",
                "display_name": "Owner",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 201, response.text
        login(result)
        yield result


def upload(client: TestClient, kind: str, rows: list[dict]) -> dict:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    headers = list(DL if kind == "dl" else CT)
    sheet.append(headers)
    for row in rows:
        values = dict.fromkeys(headers)
        values.update(
            {"Code Requisition": "T11234", "Code Local Grade": "B1", "JD": "SQL"}
            if kind == "dl"
            else {
                "ShortName": "Alex",
                "Email Address": " Alex@Example.com ",
                "Grade": "B1",
                "Bench Status": "Available",
                "Group": "RG1",
                "Skills": "SQL",
            }
        )
        values.update(row)
        sheet.append([values[k] for k in headers])
    stream = BytesIO()
    book.save(stream)
    book.close()
    response = client.post(
        f"/api/v1/uploads/{kind}", files={"file": (f"{kind}.xlsx", stream.getvalue())}
    )
    assert response.status_code == 200, response.text
    return response.json()


def ingest(client: TestClient, kind: str, rows: list[dict]) -> dict:
    run = upload(client, kind, rows)
    assert run["status"] == "preview", run
    response = client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit")
    assert response.status_code == 200, response.text
    return response.json()


def test_ct_ingestion_email_confirmation(client: TestClient) -> None:
    assert (
        client.post("/api/v1/occ/internal-domains", json={"domain": "example.com"}).status_code
        == 201
    )
    ingest(client, "ct", [{}, {"ShortName": "No email", "Email Address": None}])
    with database(client.app.state.settings) as db:
        email = db.execute("SELECT source,confirmed FROM candidate_emails").fetchone()
        assert tuple(email) == ("CT", 0)
        assert [
            row[0]
            for row in db.execute("SELECT classification FROM candidates ORDER BY candidate_id")
        ] == ["Internal", "Unknown"]
        event = db.execute(
            "SELECT details_json FROM audit_log WHERE action='CLASSIFICATION_REEVALUATED'"
        ).fetchone()
        assert json.loads(event[0])["changed"] == 2


def queue(client: TestClient) -> list[dict]:
    response = client.get("/api/v1/occ/approvals")
    assert response.status_code == 200, response.text
    return response.json()


def decision(client: TestClient, entry: dict, action: str, **kwargs) -> dict:
    collection = "demands" if entry["source"] == "DL" else "resources"
    response = client.put(
        f"/api/v1/occ/{collection}/{entry['targetId']}", json={"action": action, **kwargs}
    )
    assert response.status_code == 200, response.text
    return response.json()


def row(client: TestClient, entity: str, entity_id: int = 1) -> dict:
    with database(client.app.state.settings) as db:
        return dict(
            db.execute(f"SELECT * FROM {entity}s WHERE {entity}_id=?", (entity_id,)).fetchone()
        )


@pytest.mark.parametrize(
    "kind,entity,change_field,changed",
    [("dl", "demand", "Account Name", "Changed"), ("ct", "resource", "Location", "Changed")],
)
def test_req_ing_03_queue_new_updated_removed_and_decided_history(
    client, kind, entity, change_field, changed
):
    assert queue(client) == []
    run = upload(client, kind, [{}])
    assert queue(client) == []
    assert (
        client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit").status_code == 200
    )
    first = queue(client)[0]
    assert first["change"] == "New" and first["state"] == "Pending"
    assert first["uploadedBy"] == "Owner" and not first["conflict"]
    assert all("adminValue" not in f for f in first["fields"])
    approved = decision(client, first, "approve")
    assert approved["state"] == "Approved"
    assert row(client, entity)["occ_reviewed_by"] == 1
    ingest(client, kind, [{change_field: changed}])
    updated = queue(client)[0]
    assert updated["change"] == "Updated" and not updated["conflict"]
    assert row(client, entity)["occ_reviewed_at"] is None
    decision(client, updated, "approve")
    other = (
        {"Code Requisition": "T21234"}
        if kind == "dl"
        else {"ShortName": "Other", "Email Address": "other@example.com"}
    )
    ingest(client, kind, [other])
    entries = queue(client)
    removed = next(e for e in entries if e["targetId"] == 1 and e["state"] == "Pending")
    assert removed["change"] == "Removed"
    assert len(entries) == 4
    assert sum(e["state"] == "Approved" for e in entries) == 2
    response = client.get(f"/api/v1/occ/{entity}s/1")
    assert response.json() == removed
    with database(client.app.state.settings) as db:
        assert verify_chain(db)


@pytest.mark.parametrize(
    "kind,field,api_field", [("dl", "Account Name", "accountName"), ("ct", "Location", "location")]
)
def test_req_ing_03_pending_conflict_retains_original_before(client, kind, field, api_field):
    ingest(client, kind, [{field: "Original"}])
    decision(client, queue(client)[0], "approve")
    ingest(client, kind, [{field: "First"}])
    entry = queue(client)[0]
    ingest(client, kind, [{field: "Second"}])
    pending = [e for e in queue(client) if e["state"] == "Pending"]
    assert len(pending) == 1 and pending[0]["entryId"] == entry["entryId"]
    assert pending[0]["conflict"]
    assert next(f for f in pending[0]["fields"] if f["field"] == api_field) == {
        "field": api_field,
        "before": "Original",
        "after": "Second",
    }
    decision(client, pending[0], "revert")
    assert (
        row(client, "demand" if kind == "dl" else "resource")[
            "account_name" if kind == "dl" else "location"
        ]
        == "Original"
    )


@pytest.mark.parametrize(
    "kind,entity,field,api_field",
    [("dl", "demand", "Account Name", "accountName"), ("ct", "resource", "Location", "location")],
)
def test_req_role_03_modify_and_conflict_with_admin_value(client, kind, entity, field, api_field):
    ingest(client, kind, [{field: "Uploaded"}])
    modified = decision(
        client,
        queue(client)[0],
        "modify",
        comment="Corrected by Admin",
        values={api_field: "Admin"},
    )
    assert modified["state"] == "Modified" and modified["comment"] == "Corrected by Admin"
    assert next(f for f in modified["fields"] if f["field"] == api_field)["adminValue"] == "Admin"
    stored = row(client, entity)
    assert stored["account_name" if kind == "dl" else "location"] == "Admin"
    assert stored["occ_reviewed_by"] == 1
    ingest(client, kind, [{field: "Next upload"}])
    pending = queue(client)[0]
    assert pending["conflict"]
    assert next(f for f in pending["fields"] if f["field"] == api_field)["adminValue"] == "Admin"


@pytest.mark.parametrize(
    "kind,entity,retired", [("dl", "demand", "closed"), ("ct", "resource", "archived")]
)
def test_req_role_03_revert_new_retires_without_deletion(client, kind, entity, retired):
    ingest(client, kind, [{}])
    assert decision(client, queue(client)[0], "revert")["state"] == "Reverted"
    assert row(client, entity)["lifecycle"] == retired
    with database(client.app.state.settings) as db:
        assert verify_chain(db)
        assert (
            db.execute("SELECT count(*) FROM audit_log WHERE action='OCC_REVERT'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "kind,entity,field,db_field",
    [("dl", "demand", "Account Name", "account_name"), ("ct", "resource", "Location", "location")],
)
def test_req_role_03_revert_updated_restores_stored_fields(client, kind, entity, field, db_field):
    ingest(client, kind, [{field: "Before"}])
    decision(client, queue(client)[0], "approve")
    old = row(client, entity)
    ingest(client, kind, [{field: "After", **({"JD": "Changed JD"} if kind == "dl" else {})}])
    decision(client, queue(client)[0], "revert")
    restored = row(client, entity)
    assert restored[db_field] == "Before" and restored["row_hash"] == old["row_hash"]
    if kind == "dl":
        assert restored["current_version_id"] == old["current_version_id"]


@pytest.mark.parametrize(
    "kind,entity,active", [("dl", "demand", "open"), ("ct", "resource", "active")]
)
def test_req_role_03_revert_removed_restores_lifecycle(client, kind, entity, active):
    ingest(client, kind, [{}])
    decision(client, queue(client)[0], "approve")
    other = (
        {"Code Requisition": "T21234"}
        if kind == "dl"
        else {"ShortName": "Other", "Email Address": "other@example.com"}
    )
    ingest(client, kind, [other])
    removed = next(e for e in queue(client) if e["change"] == "Removed")
    decision(client, removed, "revert")
    assert row(client, entity)["lifecycle"] == active


def test_req_role_03_resource_modify_recomputes_eligibility_and_never_email(client):
    ingest(client, "ct", [{}])
    resource = row(client, "resource")
    assert resource["eligibility"] == "eligible"
    entry = queue(client)[0]
    for field in [
        "emailAddress",
        "email_address",
        "emailKey",
        "shortName",
        "shortNameKey",
        "rowHash",
        "candidateId",
        "lifecycle",
    ]:
        response = client.put(
            "/api/v1/occ/resources/1",
            json={"action": "modify", "comment": "reason", "values": {field: "forbidden"}},
        )
        assert response.status_code == 422, response.text
    modified = decision(
        client,
        entry,
        "modify",
        comment="Status confirmed",
        values={"benchStatus": "Selected", "benchEndDate": "2026-02-01"},
    )
    assert modified["state"] == "Modified"
    result = row(client, "resource")
    assert result["eligibility"] == "excluded" and result["exclusion_reason"]
    assert result["bench_status_code"] == "SELECTED"
    assert result["bench_end_date_parse_status"] == "valid"
    assert result["email_address"] == resource["email_address"] == " Alex@Example.com "
    assert "Alex@Example.com" not in json.dumps(queue(client))
    with database(client.app.state.settings) as db:
        assert verify_chain(db)
        assert (
            "Alex@Example.com"
            not in db.execute(
                "SELECT details_json FROM audit_log WHERE action='OCC_MODIFY'"
            ).fetchone()[0]
        )


@pytest.mark.parametrize(
    "values",
    [
        {"codeRequisition": "T29999"},
        {"rowHash": "x"},
        {"positionStartDate": "not a date"},
        {"codeLocalGrade": "not-a-grade"},
    ],
)
def test_req_role_03_invalid_demand_modification_is_atomic(client, values):
    ingest(client, "dl", [{}])
    original = row(client, "demand")
    response = client.put(
        "/api/v1/occ/demands/1", json={"action": "modify", "comment": "reason", "values": values}
    )
    assert response.status_code == 422
    assert row(client, "demand") == original
    assert queue(client)[0]["state"] == "Pending"


def test_req_role_03_errors_roles_csrf_and_audit(client):
    assert client.get("/api/v1/occ/demands/999").status_code == 404
    assert client.put("/api/v1/occ/resources/999", json={"action": "approve"}).status_code == 404
    ingest(client, "dl", [{}])
    for payload in [
        {"action": "modify"},
        {"action": "modify", "comment": " ", "values": {"bu": "BU"}},
        {"action": "approve", "values": {"bu": "BU"}},
        {"action": "invalid"},
    ]:
        assert client.put("/api/v1/occ/demands/1", json=payload).status_code == 422
    token = client.headers.pop("X-CSRF-Token")
    assert client.put("/api/v1/occ/demands/1", json={"action": "approve"}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    login(client, "Analyst")
    assert client.get("/api/v1/occ/approvals").status_code == 403
    assert client.get("/api/v1/occ/demands/1").status_code == 403
    assert client.put("/api/v1/occ/demands/1", json={"action": "approve"}).status_code == 403
    login(client)
    decision(client, queue(client)[0], "approve")
    assert client.put("/api/v1/occ/demands/1", json={"action": "approve"}).status_code == 409
    with database(client.app.state.settings) as db:
        assert verify_chain(db)
        assert (
            db.execute("SELECT count(*) FROM audit_log WHERE action='OCC_APPROVE'").fetchone()[0]
            == 1
        )
    client.cookies.clear()
    assert client.get("/api/v1/occ/approvals").status_code == 401


def test_req_ing_03_failed_apply_rolls_back_queue(client, monkeypatch):
    run = upload(client, "dl", [{}])
    original = service.apply_preview

    def fail(*args):
        original(*args)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(service, "apply_preview", fail)
    assert (
        client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit").status_code == 500
    )
    assert queue(client) == []
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM demands").fetchone()[0] == 0


def test_req_in_23_candidate_masks_email_phone_and_ct_options(client):
    ingest(
        client,
        "ct",
        [
            {"Email Address": None},
            {},
            {"ShortName": "Unrelated", "Email Address": "unrelated@example.com"},
        ],
    )
    with database(client.app.state.settings) as db:
        db.execute("UPDATE candidates SET phone='+441234567890' WHERE candidate_id=1")
        db.execute(
            "INSERT INTO candidate_emails "
            "VALUES (1,'cv.alex@example.com','cv.alex@example.com','CV',0)"
        )
    response = client.get("/api/v1/candidates/1")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["candidateId"] == 1 and detail["internalRef"] == "PFC-C-000001"
    assert detail["identityStatus"] == "incomplete" and detail["phoneMasked"] == "***90"
    assert detail["emails"] == ["cv*****@example.com"]
    assert detail["resource"]["candidateId"] == 1
    assert detail["ctEmailOptions"] == [
        {"resourceId": 2, "label": "Alex", "emailMasked": " A***@Example.com "}
    ]
    assert detail["cvVersions"] == detail["matches"] == detail["activity"] == []
    for pii in ["Alex@Example.com", "cv.alex@example.com", "+441234567890"]:
        assert pii not in response.text


@pytest.mark.parametrize(
    "action,status", [("generate_internal_id", "complete"), ("review_later", "review_later")]
)
def test_req_in_23_25_analyst_can_resolve_separate_identity_without_email(client, action, status):
    ingest(client, "ct", [{"Email Address": None}])
    login(client, "Analyst")
    response = client.put("/api/v1/candidates/1/identity", json={"action": action})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["identityStatus"] == status and result["internalRef"] == "PFC-C-000001"
    assert result["emails"] == [] and result["resource"]["emailAddressMasked"] is None
    assert result["activity"][0]["summary"] == "IDENTITY_RESOLVE"
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM candidate_emails").fetchone()[0] == 0
        assert verify_chain(db)


def test_req_in_23_select_ct_record_confirms_verbatim_email(client):
    ingest(client, "ct", [{"Email Address": None}, {}])
    login(client, "Analyst")
    response = client.put(
        "/api/v1/candidates/1/identity", json={"action": "select_ct_record", "resourceId": 2}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["identityStatus"] == "complete" and result["resource"]["resourceId"] == 2
    assert result["resource"]["emailAddressMasked"] == " A***@Example.com "
    assert "Alex@Example.com" not in response.text
    with database(client.app.state.settings) as db:
        assert (
            db.execute("SELECT candidate_id FROM resources WHERE resource_id=2").fetchone()[0] == 1
        )
        email = db.execute("SELECT * FROM candidate_emails WHERE candidate_id=1").fetchone()
        assert (email["email"], email["email_key"], email["source"], email["confirmed"]) == (
            " Alex@Example.com ",
            "alex@example.com",
            "CT",
            1,
        )
        assert (
            db.execute("SELECT count(*) FROM candidate_emails WHERE candidate_id=2").fetchone()[0]
            == 0
        )
        assert verify_chain(db)


@pytest.mark.parametrize("with_ct_email", [False, True])
def test_req_in_23_manual_identity_success_and_private_audit(client, with_ct_email):
    ingest(client, "ct", [{}] if with_ct_email else [{"Email Address": None}])
    with database(client.app.state.settings) as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM candidate_emails")]
        classification = db.execute("SELECT classification FROM candidates").fetchone()[0]
    login(client, "Analyst")
    response = client.put(
        "/api/v1/candidates/1/identity",
        json={"action": "manual_entry", "name": "  Morgan Synthetic  "},
    )
    assert response.status_code == 200, response.text
    assert response.json()["identityStatus"] == "complete"
    assert response.json()["resource"]["name"] == "Morgan Synthetic"
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT display_name FROM candidates").fetchone()[0] == "Morgan Synthetic"
        assert [tuple(row) for row in db.execute("SELECT * FROM candidate_emails")] == before
        if not with_ct_email:
            assert not db.execute("SELECT 1 FROM candidate_emails").fetchone()
        assert db.execute("SELECT classification FROM candidates").fetchone()[0] == classification
        audit = db.execute(
            "SELECT details_json FROM audit_log WHERE action='IDENTITY_RESOLVE'"
        ).fetchone()[0]
        assert "Morgan" not in audit and "Synthetic" not in audit
        assert json.loads(audit)["name_masked"] == "***"
        assert verify_chain(db)


@pytest.mark.parametrize(
    "action", ["manual_entry", "generate_internal_id", "review_later", "select_ct_record"]
)
@pytest.mark.parametrize("email", ["typed@example.com", "", None])
def test_req_in_14_23_identity_rejects_typed_email(client, action, email):
    ingest(client, "ct", [{"Email Address": None}, {}])
    body = {"action": action, "email": email}
    if action == "manual_entry":
        body["name"] = "Morgan Synthetic"
    if action == "select_ct_record":
        body["resourceId"] = 2
    response = client.put("/api/v1/candidates/1/identity", json=body)
    assert response.status_code == 422, response.text
    with database(client.app.state.settings) as db:
        assert not db.execute("SELECT 1 FROM candidate_emails WHERE candidate_id=1").fetchone()
        assert not db.execute("SELECT 1 FROM audit_log WHERE action='IDENTITY_RESOLVE'").fetchone()


@pytest.mark.parametrize("action", ["generate_internal_id", "review_later", "select_ct_record"])
@pytest.mark.parametrize("name", ["Morgan Synthetic", None])
def test_req_in_23_other_identity_actions_reject_name(client, action, name):
    ingest(client, "ct", [{"Email Address": None}, {}])
    body = {"action": action, "name": name}
    if action == "select_ct_record":
        body["resourceId"] = 2
    response = client.put("/api/v1/candidates/1/identity", json=body)
    assert response.status_code == 422, response.text


def test_req_in_23_manual_identity_validation_and_access(client):
    ingest(client, "ct", [{"Email Address": None}])
    for fields in [{}, {"name": "   "}, {"name": "X" * 201}, {"name": "Valid", "resourceId": 2}]:
        assert (
            client.put(
                "/api/v1/candidates/1/identity", json={"action": "manual_entry", **fields}
            ).status_code
            == 422
        )
    body = {"action": "manual_entry", "name": "Morgan Synthetic"}
    login(client, "Analyst")
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.put("/api/v1/candidates/1/identity", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    client.cookies.clear()
    assert client.put("/api/v1/candidates/1/identity", json=body).status_code == 401
    with database(client.app.state.settings) as db:
        assert not db.execute("SELECT 1 FROM audit_log WHERE action='IDENTITY_RESOLVE'").fetchone()


def test_req_in_25_identity_validation_conflicts_and_csrf(client):
    ingest(
        client,
        "ct",
        [{"Email Address": None}, {}, {"ShortName": "Other", "Email Address": "other@example.com"}],
    )
    assert client.get("/api/v1/candidates/999").status_code == 404
    assert (
        client.put("/api/v1/candidates/999/identity", json={"action": "review_later"}).status_code
        == 404
    )
    for body in [
        {"action": "select_ct_record"},
        {"action": "review_later", "resourceId": 2},
        {"action": "invent_email", "email": "bad@example.com"},
        {"action": "select_ct_record", "resourceId": 0},
    ]:
        assert client.put("/api/v1/candidates/1/identity", json=body).status_code == 422
    for resource_id, status in [(999, 404), (1, 422), (3, 422)]:
        assert (
            client.put(
                "/api/v1/candidates/1/identity",
                json={"action": "select_ct_record", "resourceId": resource_id},
            ).status_code
            == status
        )
    with database(client.app.state.settings) as db:
        db.execute("UPDATE candidate_emails SET confirmed=1 WHERE candidate_id=2")
    assert (
        client.put(
            "/api/v1/candidates/1/identity", json={"action": "select_ct_record", "resourceId": 2}
        ).status_code
        == 409
    )
    assert row(client, "resource", 2)["candidate_id"] == 2
    del client.headers["X-CSRF-Token"]
    assert (
        client.put("/api/v1/candidates/1/identity", json={"action": "review_later"}).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/v1/candidates/1").status_code == 401


def test_identity_resolution_reclassifies_both_email_owners(client: TestClient) -> None:
    client.post("/api/v1/occ/internal-domains", json={"domain": "example.com"})
    ingest(client, "ct", [{"Email Address": None}, {}])
    with database(client.app.state.settings) as db:
        assert [
            row[0]
            for row in db.execute("SELECT classification FROM candidates ORDER BY candidate_id")
        ] == ["Unknown", "Internal"]
    response = client.put(
        "/api/v1/candidates/1/identity",
        json={"action": "select_ct_record", "resourceId": 2},
    )
    assert response.status_code == 200, response.text
    with database(client.app.state.settings) as db:
        assert [
            row[0]
            for row in db.execute("SELECT classification FROM candidates ORDER BY candidate_id")
        ] == ["Internal", "Unknown"]
        assert tuple(
            db.execute("SELECT candidate_id,confirmed FROM candidate_emails").fetchone()
        ) == (1, 1)


def test_req_in_23_unlinked_and_standalone_candidate(client):
    ingest(client, "ct", [{}])
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO candidates(candidate_id,internal_ref,identity_status,"
            "created_at,updated_at) "
            "VALUES (2,'PFC-C-000002','incomplete','2026-01-01','2026-01-01')"
        )
        db.execute("UPDATE resources SET candidate_id=NULL")
    result = client.get("/api/v1/candidates/2").json()
    assert (
        result["resource"]["resourceId"] == 0
        and result["resource"]["eligibility"] == "insufficient"
    )
    assert result["ctEmailOptions"][0]["resourceId"] == 1
    response = client.put(
        "/api/v1/candidates/2/identity", json={"action": "select_ct_record", "resourceId": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["resource"]["resourceId"] == 1


def test_req_in_24_candidate_cv_and_match_metadata_are_masked(client):
    ingest(client, "dl", [{}])
    ingest(client, "ct", [{}])
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO resumes(resume_id,candidate_id,source_path,file_name,file_sha256,"
            "file_mtime,mime_type,discovered_at) "
            "VALUES (1,1,'alex@example.com/cv.pdf','alex@example.com.pdf','filehash',"
            "'2026-01-01','application/pdf','2026-01-01')"
        )
        db.execute(
            "INSERT INTO cv_versions(cv_version_id,resume_id,candidate_id,version_no,text_sha256,"
            "text_norm,extracted_by,identity_match,is_latest,extracted_at) "
            "VALUES (1,1,1,1,'texthash','private CV text','rule_based','email_exact',1,"
            "'2026-01-02')"
        )
        db.execute("INSERT INTO embedding_models VALUES (1,'test','onnx','hash',384,1,'v1',1)")
        db.execute(
            "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,created_at) "
            "VALUES (1,1,1,'confirmed','2026-01-01')"
        )
        template = db.execute("SELECT template_id FROM scoring_templates LIMIT 1").fetchone()[0]
        db.execute(
            "INSERT INTO match_runs(run_id,scope,template_id,embedding_model_id,engine_version,"
            "synonyms_version,config_hash,status) "
            "VALUES (1,'full',?,1,'v1','v1','hash','completed')",
            (template,),
        )
        db.execute(
            "INSERT INTO match_results(result_id,run_id,demand_id,demand_version_id,jd_req_id,"
            "resource_id,candidate_id,best_cv_version_id,score,band,rank,shortlisted,grade_relation,"
            "location_assessment,matching_evidence,gaps_checks,eligibility_evidence) "
            "VALUES (1,1,1,1,1,1,1,1,90,'Strong',1,1,'exact','Aligned',?, '[]','[]')",
            (json.dumps(["SQL evidence alex@example.com"]),),
        )
        db.execute("INSERT INTO score_breakdowns VALUES (1,'skills',60,70,'{}')")
    response = client.get("/api/v1/candidates/1")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resource"]["cvCount"] == 1 and result["resource"]["cvStatus"] == "Synced"
    assert result["cvVersions"] == [
        {
            "cvVersionId": 1,
            "fileName": "al**@example.com.pdf",
            "folderPath": "al**@example.com",
            "modifiedAt": "2026-01-01",
            "extractedAt": "2026-01-02",
            "isBest": True,
            "facts": None,
        }
    ]
    assert result["matches"][0]["score"] == 90
    assert result["matches"][0]["personEmailMasked"] == " A***@Example.com "
    assert result["matches"][0]["breakdown"] == [
        {"component": "skills", "points": 60, "maxPoints": 70}
    ]
    assert "alex@example.com" not in response.text and "private CV text" not in response.text


def test_req_ing_03_pending_unique_and_unchanged_commit_adds_no_entry(client):
    ingest(client, "dl", [{}])
    with database(client.app.state.settings) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO occ_approvals(entity_type,entity_id,ingestion_run_id,change,"
                "after_json,uploaded_at) SELECT entity_type,entity_id,ingestion_run_id,change,"
                "after_json,uploaded_at FROM occ_approvals"
            )
    ingest(client, "dl", [{}, {"Code Requisition": "T21234"}])
    assert len(queue(client)) == 2
    original = next(e for e in queue(client) if e["targetId"] == 1)
    assert not original["conflict"]


def test_req_in_25_initial_surname_match_and_confirmed_identity_rejection(client):
    ingest(client, "ct", [{"ShortName": "BSMITH", "Email Address": "bob@example.com"}])
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO candidates(candidate_id,internal_ref,display_name,identity_status,"
            "created_at,updated_at) VALUES (2,'PFC-C-000002','Bob Smith','ambiguous',"
            "'2026-01-01','2026-01-01')"
        )
        db.execute("UPDATE candidate_emails SET confirmed=1")
    result = client.get("/api/v1/candidates/2").json()
    assert result["ctEmailOptions"][0]["resourceId"] == 1
    assert (
        client.put(
            "/api/v1/candidates/2/identity", json={"action": "select_ct_record", "resourceId": 1}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "data_kind",
    [
        "none",
        "resume",
        "cv",
        "resource",
        "match",
        "cv_and_match",
        "outreach",
        "email_fk",
        "other_email",
    ],
)
def test_req_in_25_relink_requires_merge_when_old_candidate_has_data(client, data_kind):
    ingest(client, "dl", [{}])
    rows = [{"Email Address": None}, {}]
    if data_kind == "resource":
        rows.append({"ShortName": "Other", "Email Address": "other@example.com"})
    ingest(client, "ct", rows)
    with database(client.app.state.settings) as db:
        if data_kind in {"resume", "cv", "cv_and_match"}:
            db.execute(
                "INSERT INTO resumes(resume_id,candidate_id,source_path,file_name,file_sha256,"
                "mime_type,discovered_at) VALUES (1,?,'cv.pdf','cv.pdf','hash',"
                "'application/pdf','2026-01-01')",
                (2 if data_kind == "resume" else None,),
            )
            if data_kind in {"cv", "cv_and_match"}:
                db.execute(
                    "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,"
                    "text_norm,extracted_by,extracted_at) VALUES (1,2,1,'text','CV',"
                    "'rule_based','2026-01-01')"
                )
        if data_kind == "resource":
            db.execute("UPDATE resources SET candidate_id=2 WHERE resource_id=3")
        elif data_kind in {"match", "cv_and_match"}:
            db.execute("INSERT INTO embedding_models VALUES (1,'test','onnx','hash',384,1,'v1',1)")
            db.execute(
                "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,"
                "created_at) "
                "VALUES (1,1,1,'confirmed','2026-01-01')"
            )
            template = db.execute("SELECT template_id FROM scoring_templates LIMIT 1").fetchone()[0]
            db.execute(
                "INSERT INTO match_runs(run_id,scope,template_id,embedding_model_id,engine_version,"
                "synonyms_version,config_hash,status) "
                "VALUES (1,'full',?,1,'v1','v1','hash','completed')",
                (template,),
            )
            db.execute(
                "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                "resource_id,"
                "candidate_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
                "matching_evidence,gaps_checks,eligibility_evidence) "
                "VALUES (1,1,1,1,2,2,90,'Strong',1,1,'exact','Aligned','[]','[]','[]')"
            )
        elif data_kind == "email_fk":
            db.execute(
                "CREATE TABLE email_candidate_history("
                "candidate_id REFERENCES candidates(candidate_id))"
            )
            db.execute("INSERT INTO email_candidate_history VALUES (2)")
        elif data_kind == "other_email":
            db.execute(
                "INSERT INTO candidate_emails VALUES (2,'old@example.com','old@example.com','CV',0)"
            )
        elif data_kind == "outreach":
            db.execute(
                "INSERT INTO email_templates(email_template_id,name,version,subject,body_html,"
                "is_default,created_at) VALUES (999,'test',1,'Subject','Body',0,'2026-01-01')"
            )
            db.execute(
                "INSERT INTO email_messages(demand_id,resource_id,email_template_id,to_address,"
                "cc_json,subject,body_html,status,idempotency_key,created_by,"
                "created_at,updated_at) "
                "VALUES (1,2,999,' Alex@Example.com ','[]','Subject','Body','Draft','key',1,"
                "'2026-01-01','2026-01-01')"
            )
        unchanged = {
            table: [tuple(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("resources", "candidate_emails", "cv_versions", "match_results")
        }
        if data_kind == "cv_and_match":
            assert db.execute("SELECT candidate_id FROM cv_versions").fetchone()[0] == 2
            assert db.execute("SELECT candidate_id FROM match_results").fetchone()[0] == 2
    response = client.put(
        "/api/v1/candidates/1/identity", json={"action": "select_ct_record", "resourceId": 2}
    )
    if data_kind == "none":
        assert response.status_code == 200, response.text
        assert row(client, "resource", 2)["candidate_id"] == 1
    else:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "IDENTITY_MERGE_REQUIRED"
        assert row(client, "resource", 2)["candidate_id"] == 2
        with database(client.app.state.settings) as db:
            for table, before in unchanged.items():
                assert [
                    tuple(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ] == before
            assert (
                db.execute(
                    "SELECT confirmed FROM candidate_emails WHERE candidate_id=2"
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT identity_status FROM candidates WHERE candidate_id=1"
                ).fetchone()[0]
                == "incomplete"
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM audit_log WHERE action='IDENTITY_RESOLVE'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize(
    "kind,entity,field,api_field,stored_field",
    [
        ("dl", "demand", "Account Name", "accountName", "account_name"),
        ("ct", "resource", "Location", "location", "location"),
    ],
)
def test_req_ing_03_admin_modify_preserves_source_hash_and_unchanged_rows(
    client, kind, entity, field, api_field, stored_field
):
    ingest(client, kind, [{field: "Source"}])
    source_hash = row(client, entity)["row_hash"]
    decision(client, queue(client)[0], "modify", comment="Correction", values={api_field: "Admin"})
    modified = row(client, entity)
    assert modified["row_hash"] == source_hash
    other = (
        {"Code Requisition": "T21234"}
        if kind == "dl"
        else {"ShortName": "Other", "Email Address": "other@example.com"}
    )
    result = ingest(client, kind, [{field: "Source"}, other])
    assert result["counts"]["rows_unchanged"] == 1
    assert result["counts"]["rows_updated"] == 0
    assert row(client, entity) == modified
    assert not any(e["targetId"] == 1 and e["state"] == "Pending" for e in queue(client))
    ingest(client, kind, [{field: "Changed source"}, other])
    pending = next(e for e in queue(client) if e["targetId"] == 1 and e["state"] == "Pending")
    assert pending["conflict"]
    assert row(client, entity)["row_hash"] != source_hash
    decision(client, pending, "revert")
    restored = row(client, entity)
    assert restored["row_hash"] == source_hash and restored[stored_field] == "Admin"


@pytest.mark.parametrize(
    "kind,entity,field", [("dl", "demand", "Account Name"), ("ct", "resource", "Location")]
)
@pytest.mark.parametrize("change", ["Updated", "Removed"])
def test_req_ing_03_restored_upload_clears_pending_and_restores_review(
    client, kind, entity, field, change
):
    ingest(client, kind, [{field: "Original"}])
    decision(client, queue(client)[0], "approve")
    original = row(client, entity)
    other = (
        {"Code Requisition": "T21234"}
        if kind == "dl"
        else {"ShortName": "Other", "Email Address": "other@example.com"}
    )
    ingest(client, kind, [{field: "Changed"}] if change == "Updated" else [other])
    pending = next(e for e in queue(client) if e["targetId"] == 1 and e["state"] == "Pending")
    assert pending["change"] == change
    assert row(client, entity)["occ_reviewed_at"] is None
    restored_run = ingest(client, kind, [{field: "Original"}, other])
    assert not any(e["targetId"] == 1 and e["state"] == "Pending" for e in queue(client))
    restored = row(client, entity)
    assert restored["occ_reviewed_by"] == original["occ_reviewed_by"] == 1
    assert restored["occ_reviewed_at"] == original["occ_reviewed_at"]
    assert restored["row_hash"] == original["row_hash"]
    assert restored["lifecycle"] == original["lifecycle"]
    assert restored["last_ingestion_id"] == restored_run["ingestion_run_id"]
    with database(client.app.state.settings) as db:
        assert verify_chain(db)


def test_req_ing_03_restoring_admin_values_keeps_latest_source_hash(client):
    ingest(client, "dl", [{"Account Name": "Source"}])
    decision(
        client, queue(client)[0], "modify", comment="Corrected", values={"accountName": "Admin"}
    )
    before = row(client, "demand")
    ingest(client, "dl", [{"Account Name": "Changed"}])
    assert queue(client)[0]["conflict"]
    ingest(client, "dl", [{"Account Name": "Admin"}])
    assert not any(e["state"] == "Pending" for e in queue(client))
    restored = row(client, "demand")
    assert restored["account_name"] == "Admin"
    assert restored["occ_reviewed_at"] == before["occ_reviewed_at"]
    assert restored["row_hash"] != before["row_hash"]


def test_req_role_03_queue_sorts_pending_first_then_upload_time_and_id(client):
    ingest(client, "dl", [{"Code Requisition": f"T1{n:04d}"} for n in range(1, 7)])
    entries = {e["targetId"]: e for e in queue(client)}
    decision(client, entries[4], "approve")
    decision(client, entries[5], "modify", comment="Corrected", values={"bu": "Admin"})
    decision(client, entries[6], "revert")
    timestamps = {
        1: "2020-01-01T00:00:00Z",
        2: "2024-01-01T00:00:00Z",
        3: "2024-01-01T00:00:00Z",
        4: "2099-01-01T00:00:00Z",
        5: "2099-01-01T00:00:00Z",
        6: "2098-01-01T00:00:00Z",
    }
    with database(client.app.state.settings) as db:
        db.executemany(
            "UPDATE occ_approvals SET uploaded_at=? WHERE entity_id=?",
            [(ts, entity_id) for entity_id, ts in timestamps.items()],
        )
    ordered = queue(client)
    assert [e["targetId"] for e in ordered] == [3, 2, 1, 5, 4, 6]
    assert [e["state"] for e in ordered] == [
        "Pending",
        "Pending",
        "Pending",
        "Modified",
        "Approved",
        "Reverted",
    ]


def test_req_role_03_audit_contains_business_changes_but_no_emails(client):
    ingest(client, "dl", [{"Account Name": "Original"}])
    decision(client, queue(client)[0], "approve")
    ingest(client, "dl", [{"Account Name": "Uploaded"}])
    decision(
        client,
        queue(client)[0],
        "modify",
        comment="Contact owner@example.com",
        values={"accountName": "Admin"},
    )
    with database(client.app.state.settings) as db:
        approved = json.loads(
            db.execute("SELECT details_json FROM audit_log WHERE action='OCC_APPROVE'").fetchone()[
                0
            ]
        )
        assert approved["fields"]["accountName"] == {"before": None, "after": "Original"}
        modified = db.execute(
            "SELECT details_json FROM audit_log WHERE action='OCC_MODIFY'"
        ).fetchone()[0]
        assert "owner@example.com" not in modified
        assert json.loads(modified)["fields"]["accountName"] == {
            "before": "Uploaded",
            "after": "Admin",
        }
        assert verify_chain(db)
