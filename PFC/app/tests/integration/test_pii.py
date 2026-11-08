"""REQ-OUT-03, REQ-AUTH-05: audited single-field unmasking."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.db import audit, users
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.passwords import hasher
from pfc.services.pii import ANALYST_UNMASK_KEY

PASSWORD = "Strong!Random4567"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        bundled_models_dir=tmp_path / "bundled-models",
        frontend_dir=tmp_path,
        allowed_hosts=["testserver"],
        llm_enabled=False,
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings)) as result:
        with database(settings) as db:
            for email, role in [
                ("owner@example.com", 1),
                ("worker@example.com", 2),
                ("target@example.com", 2),
            ]:
                users.create_user(
                    db,
                    email=email,
                    display_name="User",
                    role_id=role,
                    password_hash=hasher(settings).hash(PASSWORD),
                    now="2026-01-01T00:00:00Z",
                    must_change_password=False,
                )
            db.execute(
                "INSERT INTO candidates(candidate_id,internal_ref,phone,identity_status,"
                "created_at,updated_at) VALUES (1,'PFC-C-1','+441234567890','complete','now','now')"
            )
            db.execute(
                "INSERT INTO candidate_emails(candidate_id,email,email_key,source,confirmed) "
                "VALUES (1,'candidate@example.com','candidate@example.com','CV',1),"
                "(1,'aaa@example.com','aaa@example.com','CT',0)"
            )
            db.execute(
                "INSERT INTO resources(resource_id,short_name,short_name_key,email_address,"
                "candidate_id,eligibility,row_hash,created_at,updated_at) "
                "VALUES (1,'Resource','resource','resource@example.com',"
                "1,'eligible','hash','now','now')"
            )
        yield result


def login(client: TestClient, role: str = "Analyst") -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={
            "role": role,
            "email": "owner@example.com" if role == "Admin" else "worker@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


@pytest.mark.parametrize(
    "entity,field,target,value,role",
    [
        ("resource", "email", 1, "resource@example.com", "Analyst"),
        ("resource", "phone", 1, "+441234567890", "Analyst"),
        ("candidate", "email", 1, "candidate@example.com", "Analyst"),
        ("candidate", "phone", 1, "+441234567890", "Analyst"),
        ("occ_user", "email", 3, "target@example.com", "Admin"),
    ],
)
def test_unmask_and_audit(
    client: TestClient, entity: str, field: str, target: int, value: str, role: str
) -> None:
    login(client, role)
    response = client.post(
        "/api/v1/pii/unmask", json={"entity": entity, "id": target, "field": field}
    )
    assert response.status_code == 200
    assert response.json() == {"value": value}
    with database(client.app.state.settings) as db:
        row = db.execute("SELECT * FROM audit_log WHERE action='UNMASK'").fetchone()
        assert row["user_id"] == (1 if role == "Admin" else 2)
        assert row["entity_type"] == entity and row["entity_id"] == str(target)
        assert json.loads(row["details_json"]) == {"field": field}
        for record in db.execute("SELECT * FROM audit_log"):
            assert all(value not in str(cell) for cell in record)
        assert audit.verify_chain(db)


def test_access_and_policy(client: TestClient) -> None:
    body = {"entity": "resource", "id": 1, "field": "email"}
    assert client.post("/api/v1/pii/unmask", json=body).status_code == 401
    login(client)
    del client.headers["X-CSRF-Token"]
    response = client.post("/api/v1/pii/unmask", json=body)
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    login(client)
    assert client.post("/api/v1/pii/unmask", json={**body, "entity": "occ_user"}).status_code == 403
    login(client, "Admin")
    response = client.put("/api/v1/occ/settings", json={ANALYST_UNMASK_KEY: False})
    assert response.status_code == 200
    assert client.get("/api/v1/occ/settings").json()[ANALYST_UNMASK_KEY] is False
    login(client)
    response = client.post("/api/v1/pii/unmask", json=body)
    assert response.status_code == 403 and response.json()["code"] == "UNMASK_DISABLED"
    assert response.headers["content-type"] == "application/problem+json"
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM audit_log WHERE action='UNMASK'").fetchone()[0] == 0
    login(client, "Admin")
    assert client.post("/api/v1/pii/unmask", json=body).status_code == 200


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({"entity": "resource", "id": 999, "field": "email"}, 404, "NOT_FOUND"),
        ({"entity": "candidate", "id": 999, "field": "email"}, 404, "NOT_FOUND"),
        ({"entity": "occ_user", "id": 999, "field": "email"}, 404, "NOT_FOUND"),
        ({"entity": "occ_user", "id": 3, "field": "phone"}, 422, "FIELD_NOT_SUPPORTED"),
        ({"entity": "other", "id": 1, "field": "email"}, 422, "VALIDATION_ERROR"),
        ({"entity": "resource", "id": 1, "field": "name"}, 422, "VALIDATION_ERROR"),
        ({"entity": "resource", "id": 1, "field": "email", "extra": True}, 422, "VALIDATION_ERROR"),
        ({"entity": "resource", "id": [1, 2], "field": "email"}, 422, "VALIDATION_ERROR"),
    ],
)
def test_invalid_requests(client: TestClient, body: dict, status: int, code: str) -> None:
    login(client, "Admin")
    response = client.post("/api/v1/pii/unmask", json=body)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM audit_log WHERE action='UNMASK'").fetchone()[0] == 0


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_values(client: TestClient, value: str | None) -> None:
    login(client)
    with database(client.app.state.settings) as db:
        db.execute("UPDATE resources SET email_address=?", (value,))
        db.execute("UPDATE candidates SET phone=?", (value,))
        db.execute("DELETE FROM candidate_emails")
    for entity, field in [
        ("resource", "email"),
        ("resource", "phone"),
        ("candidate", "email"),
        ("candidate", "phone"),
    ]:
        response = client.post(
            "/api/v1/pii/unmask", json={"entity": entity, "id": 1, "field": field}
        )
        assert response.status_code == 404
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM audit_log WHERE action='UNMASK'").fetchone()[0] == 0
