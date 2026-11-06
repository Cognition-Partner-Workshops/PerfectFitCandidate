import csv
import io
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pfc.adapters.llm.supervisor import LlamaServerSupervisor
from pfc.api.schemas.llm import LlmHealth
from pfc.config import Settings
from pfc.db import audit, users
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.passwords import hasher

PASSWORD = "Strong!Random4567"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PFC_CONFIG_DIR", str(tmp_path / "missing_config"))
    settings = Settings(
        data_dir=tmp_path,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    app = create_app(settings)
    with TestClient(app) as result:
        with database(settings) as db:
            for role_id, email in [(1, "owner@example.com"), (2, "worker@example.com")]:
                users.create_user(
                    db,
                    email=email,
                    display_name="User",
                    role_id=role_id,
                    password_hash=hasher(settings).hash(PASSWORD),
                    now="2026-01-01T00:00:00Z",
                    must_change_password=False,
                )
        yield result


def login(
    client: TestClient,
    email: str = "owner@example.com",
    role: str = "Admin",
    password: str = PASSWORD,
) -> str:
    client.cookies.clear()
    result = client.post(
        "/api/v1/auth/login", json={"email": email, "role": role, "password": password}
    )
    assert result.status_code == 200, result.text
    csrf = result.json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return csrf


def test_access_and_errors(client: TestClient) -> None:
    """REQ-ROLE-03/04: OCC requires Admin and synchroniser CSRF."""
    assert client.get("/api/v1/occ/users").status_code == 401
    login(client, "worker@example.com", "Analyst")
    assert client.get("/api/v1/occ/users").status_code == 403
    login(client)
    del client.headers["X-CSRF-Token"]
    result = client.post("/api/v1/occ/users", json={})
    assert result.status_code == 403
    assert result.headers["content-type"] == "application/problem+json"
    login(client)
    for path, body, status, code in [
        ("/users/9999", {}, 404, "NOT_FOUND"),
        ("/users/1", {"is_active": False}, 409, "LAST_ADMIN"),
        ("/users/1", {"role": "Analyst"}, 409, "LAST_ADMIN"),
        (
            "/status-map/test",
            {"canonical_code": "missing", "priority": 1, "is_excluded": False},
            422,
            "VALIDATION_ERROR",
        ),
        ("/grades", [{"code": "A"}, {"code": "A"}], 422, "VALIDATION_ERROR"),
    ]:
        result = client.put("/api/v1/occ" + path, json=body)
        assert result.status_code == status, result.text
        assert result.json()["code"] == code


def test_unchanged_role_preserves_session(client: TestClient) -> None:
    """REQ-AUTH-04: resubmitting an unchanged role must not revoke the session."""
    login(client)
    assert client.put("/api/v1/occ/users/1", json={"role": "Admin"}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    login(client, "worker@example.com", "Analyst")
    login(client)
    assert client.put("/api/v1/occ/users/2", json={"role": "Admin"}).status_code == 200
    with database(client.app.state.settings) as db:
        assert not db.execute(
            "SELECT 1 FROM sessions WHERE user_id=2 AND revoked_at IS NULL"
        ).fetchone()
    login(client, "worker@example.com", "Admin")
    login(client)
    assert client.put("/api/v1/occ/users/2", json={"is_active": False}).status_code == 200
    with database(client.app.state.settings) as db:
        assert not db.execute(
            "SELECT 1 FROM sessions WHERE user_id=2 AND revoked_at IS NULL"
        ).fetchone()


def test_users_and_password_reset(client: TestClient) -> None:
    """REQ-AUTH-02/04/05: issue passwords once; reset revokes sessions and forces change."""
    login(client)
    body = {
        "email": "New.User@EXAMPLE.com",
        "display_name": "New User",
        "role": "Analyst",
        "persona": "B",
    }
    result = client.post("/api/v1/occ/users", json=body)
    assert result.status_code == 201, result.text
    target = result.json()["user"]["user_id"]
    password = result.json()["one_time_password"]
    assert client.post("/api/v1/occ/users", json=body).status_code == 409
    assert "password_hash" not in client.get("/api/v1/occ/users").text
    assert client.post("/api/v1/occ/users/9999/unlock").status_code == 404
    assert client.post("/api/v1/occ/users/9999/reset-password").status_code == 404
    assert (
        client.put(
            f"/api/v1/occ/users/{target}", json={"display_name": "Changed", "persona": None}
        ).status_code
        == 200
    )
    with database(client.app.state.settings) as db:
        users.update_login_state(
            db,
            target,
            failed_attempts=5,
            locked_until="2099-01-01T00:00:00Z",
            now="2026-01-01T00:00:00Z",
        )
    listed = client.get("/api/v1/occ/users").json()
    assert next(u for u in listed if u["user_id"] == target)["status"] == "Locked"
    assert client.post(f"/api/v1/occ/users/{target}/unlock").json()["failed_attempts"] == 0
    result = client.post(f"/api/v1/occ/users/{target}/reset-password")
    new_password = result.json()["one_time_password"]
    assert new_password != password
    assert client.post("/api/v1/occ/users", json={**body, "role": "Unknown"}).status_code == 422
    login(client, body["email"], "Analyst", new_password)
    assert client.get("/api/v1/occ/grades").json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    result = client.post(
        "/api/v1/auth/password",
        json={"current_password": new_password, "new_password": "OtherPassword123!"},
    )
    assert result.status_code == 200
    login(client)
    admin = client.post(
        "/api/v1/occ/users",
        json={"email": "second@example.com", "display_name": "Second", "role": "Admin"},
    ).json()
    assert (
        client.put("/api/v1/occ/users/1", json={"role": "Analyst"}).json()["code"] == "SELF_ADMIN"
    )
    assert (
        client.put(
            f"/api/v1/occ/users/{admin['user']['user_id']}", json={"is_active": False}
        ).status_code
        == 200
    )
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        details = " ".join(row[0] or "" for row in db.execute("SELECT details_json FROM audit_log"))
        assert (
            body["email"] not in details and password not in details and new_password not in details
        )
        actions = {row[0] for row in db.execute("SELECT action FROM audit_log")}
        assert {"USER_CREATE", "USER_UPDATE", "USER_UNLOCK", "USER_RESET_PASSWORD"} <= actions


def test_reference_data(client: TestClient) -> None:
    """REQ-ELIG-02/03/07, REQ-GRD-01: reference writes and audit are atomic."""
    login(client)
    base = "/api/v1/occ"
    assert client.get(base + "/grades").json() == []
    bench = {"code": "AVAILABLE", "label": "Available", "is_excluded": False, "priority": 1}
    assert client.post(base + "/bench-statuses", json=bench).status_code == 201
    assert client.post(base + "/bench-statuses", json=bench).status_code == 409
    assert (
        client.post(base + "/bench-statuses", json={**bench, "priority": None}).status_code == 422
    )
    bench["label"] = "Changed"
    assert client.put(base + "/bench-statuses/AVAILABLE", json=bench).status_code == 200
    assert (
        client.put(
            base + "/status-map/Source label",
            json={"canonical_code": "AVAILABLE", "priority": 1, "is_excluded": False},
        ).status_code
        == 200
    )
    assert client.delete(base + "/bench-statuses/AVAILABLE").status_code == 409
    assert len(client.get(base + "/status-map").json()) == 1
    assert client.delete(base + "/status-map/Source label").status_code == 204
    assert client.delete(base + "/bench-statuses/AVAILABLE").status_code == 204
    assert client.delete(base + "/bench-statuses/AVAILABLE").status_code == 404
    assert client.put(base + "/grades", json=[{"code": "A"}, {"code": "B"}]).status_code == 200
    result = client.put(base + "/grades", json=[{"code": "B"}, {"code": "A", "active": False}])
    assert [g["code"] for g in result.json()] == ["B", "A"]
    assert result.json()[1]["active"] == 0
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO demands(code_requisition,id_format_ok,code_local_grade,row_hash,"
            "created_at,updated_at) VALUES ('T10001',1,'B','hash',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
    assert client.put(base + "/grades", json=[{"code": "A"}]).status_code == 409
    assert [g["code"] for g in client.get(base + "/grades").json()] == ["B", "A"]
    assert (
        client.put(
            base + "/location-aliases",
            json={"aliases": {" LDN ": " London "}, "travel_clusters": {" UK ": [" London "]}},
        ).status_code
        == 200
    )
    assert client.get(base + "/location-aliases").json() == {
        "aliases": {"ldn": "london"},
        "travel_clusters": {"uk": ["london"]},
    }
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        assert (
            db.execute("SELECT count(*) FROM audit_log WHERE action='OCC_EDIT'").fetchone()[0] == 7
        )
        assert (
            db.execute("SELECT count(*) FROM audit_log WHERE action='SETTING_CHANGE'").fetchone()[0]
            == 1
        )
        entry = db.execute(
            "SELECT details_json FROM audit_log WHERE entity_type='bench_statuses' "
            "AND action='OCC_EDIT' ORDER BY audit_id LIMIT 1"
        ).fetchone()[0]
        assert json.loads(entry)["after"]["label"] == "Available"


@pytest.mark.parametrize(
    "path",
    [
        "/synonyms",
        "/scoring-templates",
        "/email-templates",
        "/resource-team",
        "/internal-domains",
        "/settings",
        "/audit-log",
        "/audit-log/export",
    ],
)
def test_new_endpoint_rbac(client: TestClient, path: str) -> None:
    """REQ-ROLE-03/04: all OCC surfaces are Admin-only."""
    assert client.get("/api/v1/occ" + path).status_code == 401
    login(client, "worker@example.com", "Analyst")
    assert client.get("/api/v1/occ" + path).status_code == 403


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/synonyms"),
        ("PUT", "/synonyms/1"),
        ("DELETE", "/synonyms/1"),
        ("POST", "/synonyms/1/approve"),
        ("POST", "/synonyms/1/reject"),
        ("POST", "/scoring-templates"),
        ("PUT", "/scoring-templates/1"),
        ("DELETE", "/scoring-templates/1"),
        ("POST", "/scoring-templates/1/activate"),
        ("POST", "/email-templates"),
        ("POST", "/email-templates/1/set-default"),
        ("POST", "/resource-team"),
        ("PUT", "/resource-team/1"),
        ("DELETE", "/resource-team/1"),
        ("POST", "/internal-domains"),
        ("DELETE", "/internal-domains/example.com"),
        ("PUT", "/settings"),
    ],
)
def test_new_writes_csrf(client: TestClient, method: str, path: str) -> None:
    """REQ-AUTH-04: state-changing OCC requests require synchroniser CSRF."""
    login(client)
    del client.headers["X-CSRF-Token"]
    response = client.request(method, "/api/v1/occ" + path, json={})
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"


def test_synonyms(client: TestClient) -> None:
    """REQ-MAT-05: normalize, filter and review suggestions."""
    login(client)
    base = "/api/v1/occ/synonyms"
    body = {"canonical": "Python", "term": "PY.", "relation": "abbreviation"}
    first = client.post(base, json=body)
    assert first.status_code == 201, first.text
    target = first.json()["synonym_id"]
    assert first.json()["credit"] == 1
    assert client.post(base, json={**body, "term": "py"}).status_code == 409
    assert client.post(base, json={**body, "relation": "related"}).status_code == 422
    assert client.put(base + f"/{target}", json={**body, "term": "py3"}).status_code == 200
    suggestion = client.post(base, json={**body, "term": "pythn", "status": "suggested"}).json()
    suggested_id = suggestion["synonym_id"]
    assert (
        len(client.get(base, params={"status": "suggested", "skill": "Python", "q": "pyth"}).json())
        == 1
    )
    assert (
        client.post(base + f"/{suggested_id}/approve", json={"canonical": "Missing"}).status_code
        == 404
    )
    assert (
        client.post(base + f"/{suggested_id}/approve", json={"canonical": "Python"}).json()[
            "status"
        ]
        == "approved"
    )
    assert client.post(base + f"/{suggested_id}/reject").json()["code"] == "INVALID_STATE"
    rejected = client.post(base, json={**body, "term": "other", "status": "suggested"}).json()
    assert client.post(base + f"/{rejected['synonym_id']}/reject").json()["status"] == "rejected"
    assert client.delete(base + f"/{target}").status_code == 204
    assert client.delete(base + f"/{target}").status_code == 404
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log "
                "WHERE entity_type='skill_synonyms' ORDER BY audit_id LIMIT 1"
            ).fetchone()[0]
        )
        assert details["after"]["term"] == "PY."


def test_scoring_templates(client: TestClient) -> None:
    """REQ-SCR-01/02, REQ-ELIG-06: draft versions publish atomically and are immutable."""
    login(client)
    base = "/api/v1/occ/scoring-templates"
    weights = {
        "skills": 70,
        "grade": 10,
        "location": 5,
        "bench_start": 5,
        "bench_end": 5,
        "status": 5,
    }
    assert client.post(base, json={"change_note": "First"}).status_code == 404
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO scoring_templates(name,version,weights_json,rules_json,status,"
            "created_at) VALUES ('default',1,?,'{}','active','2026-01-01T00:00:00Z')",
            (json.dumps(weights),),
        )
    draft = client.post(base, json={"change_note": "Clone"}).json()
    target = draft["template_id"]
    assert draft["version"] == 2 and draft["status"] == "draft"
    assert draft["rules"]["_meta"]["change_note"] == "Clone"
    for bad in [
        {**weights, "skills": 69},
        {**weights, "capacity": 1},
        {**weights, "skills": 70.0},
        {**weights, "grade": -1},
    ]:
        assert (
            client.put(base + f"/{target}", json={"weights": bad, "change_note": "Bad"}).status_code
            == 200
        )
        assert client.post(base + f"/{target}/activate").status_code == 422
    assert (
        client.put(
            base + f"/{target}", json={"weights": weights, "change_note": "Fixed"}
        ).status_code
        == 200
    )
    assert client.post(base + f"/{target}/activate").json()["status"] == "active"
    assert client.get(base + "/1").json()["status"] == "retired"
    assert sum(row["status"] == "active" for row in client.get(base).json()) == 1
    for id in (1, target):
        assert (
            client.put(base + f"/{id}", json={"change_note": "No"}).json()["code"]
            == "TEMPLATE_IMMUTABLE"
        )
        assert client.delete(base + f"/{id}").status_code == 409
    removable = client.post(base, json={"clone_from_id": target, "change_note": "Remove"}).json()
    assert removable["version"] == 3
    assert client.delete(base + f"/{removable['template_id']}").status_code == 204
    assert client.get(base + "/99999").status_code == 404
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log "
                "WHERE entity_type='scoring_templates' ORDER BY audit_id LIMIT 1"
            ).fetchone()[0]
        )
        assert details["after"]["weights"] == weights


def test_email_templates(client: TestClient) -> None:
    """REQ-EML-03: append-only email versions with allowlisted placeholders."""
    login(client)
    base = "/api/v1/occ/email-templates"
    body = {
        "name": "candidate",
        "subject": "Opportunity {requisition}",
        "body_html": "<p>Hello {candidate_name}</p>",
    }
    assert client.get(base).json() == []
    assert (
        client.post(base, json={**body, "subject": "{unknown}"}).json()["code"]
        == "UNKNOWN_PLACEHOLDER"
    )
    first = client.post(base, json=body).json()
    second = client.post(base, json={**body, "subject": "{role}"}).json()
    assert (first["version"], second["version"]) == (1, 2)
    assert len(client.get(base).json()) == 1
    assert len(client.get(base, params={"all": True}).json()) == 2
    for row in (first, second):
        assert client.post(base + f"/{row['email_template_id']}/set-default").status_code == 200
    assert sum(r["is_default"] for r in client.get(base, params={"all": True}).json()) == 1
    assert client.get(base + f"/{first['email_template_id']}").json()["subject"] == body["subject"]
    assert client.get(base + "/99999").status_code == 404
    assert client.post(base + "/99999/set-default").status_code == 404


@pytest.mark.parametrize(
    "body_html",
    [
        "<style>p { color: red }</style><p>{candidate_name}</p>",
        "<style>a{color:red}</style>",
        "{candidate_name}",
        "{{",
        "{{literal_braces}}",
        "{unfinished",
    ],
)
def test_email_literal_braces(client: TestClient, body_html: str) -> None:
    """REQ-EML-03: CSS and literal braces are not placeholder identifiers."""
    login(client)
    result = client.post(
        "/api/v1/occ/email-templates",
        json={
            "name": "styled",
            "subject": "Opportunity {role}",
            "body_html": body_html,
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["body_html"] == body_html


@pytest.mark.parametrize("token", ["{Unknown}", "{missing!r}"])
@pytest.mark.parametrize("field", ["subject", "body_html"])
def test_email_unknown_brace_tokens(client: TestClient, token: str, field: str) -> None:
    """REQ-EML-03: every unescaped placeholder token must be allowlisted."""
    login(client)
    body = {"name": "invalid", "subject": "{role}", "body_html": "{candidate_name}"}
    body[field] = token
    response = client.post("/api/v1/occ/email-templates", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "UNKNOWN_PLACEHOLDER"


def test_recipients_domains_settings_and_audit(client: TestClient) -> None:
    """REQ-EML-05, REQ-CLS-01, REQ-ROLE-04, REQ-AUTH-05: safe audited administration."""
    login(client)
    base = "/api/v1/occ"
    recipient = client.post(
        base + "/resource-team", json={"email": "Team@EXAMPLE.com", "display_name": "Team"}
    ).json()
    target = recipient["recipient_id"]
    assert recipient["email"] == "Team@EXAMPLE.com"
    assert (
        client.post(base + "/resource-team", json={"email": "team@example.com"}).status_code == 409
    )
    assert client.post(base + "/resource-team", json={"email": "bad"}).status_code == 422
    assert (
        client.put(base + f"/resource-team/{target}", json={"is_active": False}).status_code == 200
    )
    assert len(client.get(base + "/resource-team").json()) == 1
    assert client.delete(base + f"/resource-team/{target}").status_code == 204
    assert client.put(base + "/resource-team/999", json={"is_active": True}).status_code == 404
    assert (
        client.post(base + "/internal-domains", json={"domain": " @Example.COM "}).json()["domain"]
        == "example.com"
    )
    assert (
        client.post(base + "/internal-domains", json={"domain": "example.com"}).status_code == 409
    )
    assert client.post(base + "/internal-domains", json={"domain": "bad domain"}).status_code == 422
    assert len(client.get(base + "/internal-domains").json()) == 1
    assert client.delete(base + "/internal-domains/example.com").status_code == 204
    assert client.delete(base + "/internal-domains/example.com").status_code == 404
    assert (
        client.put(
            base + "/settings", json={"matching.top_n": 5, "resume.options": {"mode": "local"}}
        ).status_code
        == 200
    )
    assert client.put(base + "/settings", json={"matching.top_n": 7}).status_code == 200
    assert client.get(base + "/settings").json()["matching.top_n"] == 7
    for key in ("bad", "location.aliases"):
        assert client.put(base + "/settings", json={key: 1}).status_code == 422
    assert client.put(base + "/settings", json={"smtp.password": "sensitive"}).status_code == 200
    history = client.get(
        base + "/audit-log", params={"action": ["SETTING_CHANGE"], "entity_id": "matching.top_n"}
    ).json()
    assert history["chain_valid"] and history["total"] == 2
    assert history["items"][0]["action"] == "SETTING_CHANGE"
    details = json.loads(history["items"][0]["details_json"])
    assert details == {"before": {"value": 5}, "after": {"value": 7}}
    with database(client.app.state.settings) as db:
        audit.append_audit(
            db, ts="2026-01-01T00:00:00Z", action="=SUM(1,2)", entity_type="+type", entity_id="@id"
        )
    exported = client.get(base + "/audit-log/export", params={"format": "csv"})
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    injected = next(r for r in rows if "SUM" in r["action"])
    assert injected["action"] == "'=SUM(1,2)"
    assert injected["entity_type"] == "'+type"
    assert injected["entity_id"] == "'@id"
    filtered = client.get(
        base + "/audit-log",
        params={
            "action": ["=SUM(1,2)", "NONEXISTENT"],
            "entity_type": "+type",
            "entity_id": "@id",
            "from": "2025-12-31T00:00:00Z",
            "to": "2026-01-02T00:00:00Z",
        },
    ).json()
    assert filtered["total"] == 1 and filtered["chain_valid"]
    assert client.get(base + "/audit-log", params={"limit": 1, "offset": 1}).json()["items"]
    del client.headers["X-CSRF-Token"]
    assert client.get(base + "/audit-log/export", params={"format": "csv"}).status_code == 200
    assert client.get(base + "/audit-log", params={"limit": 1001}).status_code == 422
    assert client.get(base + "/audit-log", params={"from": "bad"}).status_code == 422
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        all_details = " ".join(r[0] or "" for r in db.execute("SELECT details_json FROM audit_log"))
        assert "Team@EXAMPLE.com" not in all_details
        assert "sensitive" not in all_details
        assert db.execute("SELECT 1 FROM audit_log WHERE action='AUDIT_EXPORT'").fetchone()


@pytest.mark.parametrize(
    "path",
    [
        "/occ/llm-models",
        "/llm/models",
        "/llm/active",
        "/llm/health",
        "/settings/me",
    ],
)
def test_llm_settings_anonymous(client: TestClient, path: str) -> None:
    """REQ-AUTH-01, REQ-ROLE-04: all model/settings reads require authentication."""
    response = client.get("/api/v1" + path)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_model_approvals_and_csrf(client: TestClient) -> None:
    """REQ-LLM-01/06: Admin approval, static list and active-model protection."""
    login(client, "worker@example.com", "Analyst")
    assert client.get("/api/v1/occ/llm-models").status_code == 403
    assert client.put("/api/v1/occ/llm-models/1", json={"approved": True}).status_code == 403
    assert client.get("/api/v1/llm/models").json() == []
    assert client.get("/api/v1/llm/active").json() is None
    login(client)
    models = client.get("/api/v1/occ/llm-models").json()
    assert len(models) == 2
    target = models[0]["model_id"]
    assert models[0]["file_sha256"] == "" and models[0]["size_mb"] == 0
    del client.headers["X-CSRF-Token"]
    denied = client.put(f"/api/v1/occ/llm-models/{target}", json={"approved": True})
    assert denied.status_code == 403
    assert denied.headers["content-type"] == "application/problem+json"
    login(client)
    assert (
        client.put(f"/api/v1/occ/llm-models/{target}", json={"approved": True}).status_code == 200
    )
    assert len(client.get("/api/v1/llm/models").json()) == 1
    assert (
        client.put("/api/v1/occ/llm-models/999", json={"approved": True}).json()["code"]
        == "NOT_FOUND"
    )
    assert client.put(f"/api/v1/occ/llm-models/{target}", json={}).status_code == 422
    assert client.post("/api/v1/occ/llm-models", json={}).status_code == 405
    assert client.delete(f"/api/v1/occ/llm-models/{target}").status_code == 405
    with database(client.app.state.settings) as db:
        db.execute("UPDATE llm_models SET is_active=1 WHERE model_id=?", (target,))
    conflict = client.put(f"/api/v1/occ/llm-models/{target}", json={"approved": False})
    assert conflict.status_code == 409 and conflict.json()["code"] == "MODEL_ACTIVE"
    assert client.get("/api/v1/llm/active").json()["model_id"] == target
    with database(client.app.state.settings) as db:
        detail = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE entity_type='llm_models'"
            ).fetchone()[0]
        )
        assert detail["before"]["approved"] == 0 and detail["after"]["approved"] == 1
        assert audit.verify_chain(db)


def test_per_user_active_model(client: TestClient) -> None:
    """REQ-LLM-06: independent choices, shared validation and global fallback."""
    with database(client.app.state.settings) as db:
        db.execute("UPDATE llm_models SET approved=1")
        db.execute("UPDATE llm_models SET is_active=1 WHERE model_id=1")
    login(client, "worker@example.com", "Analyst")
    assert client.get("/api/v1/llm/active").json()["model_id"] == 1
    del client.headers["X-CSRF-Token"]
    assert client.put("/api/v1/llm/active", json={"model_id": 2}).status_code == 403
    login(client, "worker@example.com", "Analyst")
    assert (
        client.put("/api/v1/llm/active", json={"model_id": 999}).json()["code"]
        == "MODEL_NOT_APPROVED"
    )
    assert client.put("/api/v1/llm/active", json={"model_id": 0}).status_code == 422
    assert client.put("/api/v1/llm/active", json={"model_id": 2}).json()["model_id"] == 2
    assert client.get("/api/v1/settings/me").json()["active_model_id"] == 2
    login(client)
    assert client.get("/api/v1/llm/active").json()["model_id"] == 1
    assert client.put("/api/v1/occ/llm-models/2", json={"approved": False}).status_code == 200
    login(client, "worker@example.com", "Analyst")
    assert client.get("/api/v1/llm/active").json()["model_id"] == 1
    assert (
        client.put("/api/v1/llm/active", json={"model_id": 2}).json()["code"]
        == "MODEL_NOT_APPROVED"
    )
    with database(client.app.state.settings) as db:
        choice = db.execute(
            "SELECT * FROM settings WHERE scope='user' AND key='llm.active_model'"
        ).fetchone()
        assert choice["user_id"] == 2 and json.loads(choice["value_json"]) == 2
        assert audit.verify_chain(db)


def test_clear_active_model_choice(client: TestClient) -> None:
    """REQ-LLM-06: null restores the instance default and audits the cleared choice."""
    with database(client.app.state.settings) as db:
        db.execute("UPDATE llm_models SET approved=1")
        db.execute("UPDATE llm_models SET is_active=1 WHERE model_id=1")
    login(client, "worker@example.com", "Analyst")
    assert client.put("/api/v1/llm/active", json={"model_id": 2}).status_code == 200
    cleared = client.put("/api/v1/llm/active", json={"model_id": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["model_id"] == 1
    assert client.get("/api/v1/llm/active").json()["model_id"] == 1
    assert client.get("/api/v1/settings/me").json()["active_model_id"] == 1
    assert client.put("/api/v1/llm/active", json={}).status_code == 422
    with database(client.app.state.settings) as db:
        assert not db.execute(
            "SELECT 1 FROM settings WHERE user_id=2 AND key='llm.active_model'"
        ).fetchone()
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE action='SETTING_CHANGE' "
                "AND entity_id='user:2:llm.active_model' ORDER BY audit_id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert details == {"before": {"value": 2}, "after": None}
        assert audit.verify_chain(db)


def test_model_approval_schedules_after_commit(client: TestClient, tmp_path: Path) -> None:
    """REQ-LLM-06: the first approval starts inference and unapproval re-resolves it."""
    settings = client.app.state.settings
    settings.models_dir = tmp_path
    (tmp_path / "one.gguf").touch()
    with database(settings) as db:
        db.execute("UPDATE llm_models SET file_name='one.gguf' WHERE model_id=1")

    class Probe(LlamaServerSupervisor):
        selections: list[tuple[str, Path] | None] = []

        def schedule(self, model: tuple[str, Path] | None) -> None:
            self.selections.append(model)
            self.state = "ready" if model else "unavailable"
            self.proc = Mock()
            self.proc.poll.return_value = None if model else 0
            if model:
                self.model_name, self.model_path = model

    probe = Probe(settings)
    client.app.state.llm_supervisor = probe
    login(client)
    assert client.put("/api/v1/occ/llm-models/1", json={"approved": True}).status_code == 200
    assert probe.selections == [("Qwen3-4B-Instruct-2507", (tmp_path / "one.gguf").resolve())]
    assert client.put("/api/v1/occ/llm-models/1", json={"approved": False}).status_code == 200
    assert probe.selections[-1] is None
    assert probe.model_name == "Qwen3-4B-Instruct-2507"
    assert client.put("/api/v1/occ/llm-models/1", json={"approved": True}).status_code == 200
    assert len(probe.selections) == 3
    assert probe.selections[-1] == ("Qwen3-4B-Instruct-2507", (tmp_path / "one.gguf").resolve())
    assert client.put("/api/v1/occ/llm-models/999", json={"approved": True}).status_code == 404
    assert len(probe.selections) == 3


@pytest.fixture
def default_models(client: TestClient, tmp_path: Path) -> LlamaServerSupervisor:
    settings = client.app.state.settings
    settings.models_dir = tmp_path
    for name in ("one", "embedding", "three", "four"):
        (tmp_path / f"{name}.gguf").touch()
    with database(settings) as db:
        db.execute(
            "UPDATE llm_models SET approved=1,file_name='one.gguf',is_active=1 WHERE model_id=1"
        )
        db.execute("UPDATE llm_models SET approved=1,file_name='embedding.gguf' WHERE model_id=2")
        for name, filename, approved in [
            ("generation-three", "three.gguf", 1),
            ("generation-four", "four.gguf", 0),
            ("generation-missing", "", 1),
        ]:
            db.execute(
                "INSERT INTO llm_models(name,file_name,file_sha256,size_mb,ram_estimate_mb,"
                "context_tokens,licence,approved) VALUES (?,?, '',1024,2048,4096,'test',?)",
                (name, filename, approved),
            )

    class Probe(LlamaServerSupervisor):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self.selections: list[tuple[str, Path] | None] = []
            self.model_name = "Qwen3-4B-Instruct-2507"
            self.model_path = (tmp_path / "one.gguf").resolve()
            self.state = "ready"
            self.proc = Mock()
            self.proc.poll.return_value = None

        def schedule(self, model: tuple[str, Path] | None) -> None:
            self.selections.append(model)
            self.model_name, self.model_path = model if model else (None, None)

    probe = Probe(settings)
    client.app.state.llm_supervisor = probe
    return probe


def test_occ_make_default_transaction_audit_and_wire(
    client: TestClient, default_models: LlamaServerSupervisor, tmp_path: Path
) -> None:
    login(client)
    response = client.put("/api/v1/occ/llm-models/3", json={"isDefault": True})
    assert response.status_code == 200, response.text
    models = response.json()
    assert isinstance(models, list)
    assert [row["id"] for row in models if row["isDefault"]] == ["3"]
    assert all(
        {
            "id",
            "name",
            "purpose",
            "ramGb",
            "diskGb",
            "licence",
            "approved",
            "isDefault",
        }
        <= row.keys()
        for row in models
    )
    assert models[2]["ramGb"] == 2 and models[2]["diskGb"] == 1
    assert models == client.get("/api/v1/occ/llm-models").json()
    assert default_models.selections == [("generation-three", (tmp_path / "three.gguf").resolve())]
    with database(client.app.state.settings) as db:
        assert [r[0] for r in db.execute("SELECT model_id FROM llm_models WHERE is_active=1")] == [
            3
        ]
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE action='OCC_EDIT' "
                "AND entity_type='llm_models' AND entity_id='default'"
            ).fetchone()[0]
        )
        assert details == {"before": {"default_model_id": 1}, "after": {"default_model_id": 3}}
        assert audit.verify_chain(db)
    assert client.put("/api/v1/occ/llm-models/1", json={"isDefault": True}).status_code == 200
    assert [m["id"] for m in client.get("/api/v1/occ/llm-models").json() if m["isDefault"]] == ["1"]


def test_occ_default_preserves_personal_selection(
    client: TestClient, default_models: LlamaServerSupervisor
) -> None:
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO settings(scope,user_id,key,value_json,updated_by,updated_at) "
            "VALUES ('user',2,'llm.active_model','1',2,'2026-01-01T00:00:00Z')"
        )
        before = [tuple(r) for r in db.execute("SELECT * FROM settings WHERE scope='user'")]
    login(client)
    assert client.put("/api/v1/occ/llm-models/3", json={"isDefault": True}).status_code == 200
    assert default_models.selections == []
    with database(client.app.state.settings) as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM settings WHERE scope='user'")] == before
        assert db.execute("SELECT model_id FROM llm_models WHERE is_active=1").fetchone()[0] == 3
        assert default_models.resolve_model(db)[0] == "Qwen3-4B-Instruct-2507"


@pytest.mark.parametrize(
    "target,body,code",
    [
        (4, {"isDefault": True}, "MODEL_NOT_APPROVED"),
        (2, {"isDefault": True}, "MODEL_NOT_GENERATIVE"),
        (5, {"isDefault": True}, "MODEL_NOT_GENERATIVE"),
        (3, {"isDefault": False}, "VALIDATION_ERROR"),
        (3, {"isDefault": None}, "VALIDATION_ERROR"),
        (3, {}, "VALIDATION_ERROR"),
        (3, {"approved": False, "isDefault": True}, "MODEL_NOT_APPROVED"),
        (3, {"approved": None}, "VALIDATION_ERROR"),
    ],
)
def test_occ_invalid_default_no_changes(
    client: TestClient, default_models: LlamaServerSupervisor, target: int, body: dict, code: str
) -> None:
    login(client)
    with database(client.app.state.settings) as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM llm_models ORDER BY model_id")]
        audits = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    response = client.put(f"/api/v1/occ/llm-models/{target}", json=body)
    assert response.status_code == 422 and response.json()["code"] == code
    with database(client.app.state.settings) as db:
        assert [
            tuple(r) for r in db.execute("SELECT * FROM llm_models ORDER BY model_id")
        ] == before
        assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits
    assert default_models.selections == []


def test_occ_default_auth_and_combined_approval(
    client: TestClient, default_models: LlamaServerSupervisor
) -> None:
    assert client.put("/api/v1/occ/llm-models/3", json={"isDefault": True}).status_code == 401
    login(client, "worker@example.com", "Analyst")
    assert client.put("/api/v1/occ/llm-models/3", json={"isDefault": True}).status_code == 403
    login(client)
    del client.headers["X-CSRF-Token"]
    assert client.put("/api/v1/occ/llm-models/3", json={"isDefault": True}).status_code == 403
    login(client)
    response = client.put("/api/v1/occ/llm-models/4", json={"approved": True, "isDefault": True})
    assert response.status_code == 200
    assert [m["id"] for m in response.json() if m["isDefault"]] == ["4"]
    assert next(m for m in response.json() if m["id"] == "4")["approved"] is True
    conflict = client.put("/api/v1/occ/llm-models/4", json={"approved": False})
    assert conflict.status_code == 409 and conflict.json()["code"] == "MODEL_ACTIVE"
    assert client.put("/api/v1/occ/llm-models/999", json={"isDefault": True}).status_code == 404


def test_model_updates_schedule_after_commit(client: TestClient, tmp_path: Path) -> None:
    """REQ-LLM-06: both settings surfaces schedule switches only after a committed choice."""
    settings = client.app.state.settings
    settings.models_dir = tmp_path
    for name in ("one", "two"):
        (tmp_path / f"{name}.gguf").touch()
    with database(settings) as db:
        db.execute("UPDATE llm_models SET approved=1,file_name='one.gguf' WHERE model_id=1")
        db.execute("UPDATE llm_models SET approved=1,file_name='two.gguf' WHERE model_id=2")

    class Probe(LlamaServerSupervisor):
        selections: list[tuple[str, Path] | None] = []

        def schedule(self, model: tuple[str, Path] | None) -> None:
            self.selections.append(model)

    probe = Probe(settings)
    client.app.state.llm_supervisor = probe
    login(client, "worker@example.com", "Analyst")
    assert client.put("/api/v1/llm/active", json={"model_id": 2}).status_code == 200
    assert probe.selections == [("nomic-embed-text-v1.5", (tmp_path / "two.gguf").resolve())]
    assert client.put("/api/v1/settings/me", json={"active_model_id": 1}).status_code == 200
    assert probe.selections[-1] == ("Qwen3-4B-Instruct-2507", (tmp_path / "one.gguf").resolve())
    assert client.put("/api/v1/settings/me", json={"active_model_id": 1}).status_code == 200
    assert len(probe.selections) == 3
    assert client.put("/api/v1/settings/me", json={"density": "compact"}).status_code == 200
    assert len(probe.selections) == 3
    assert client.put("/api/v1/llm/active", json={"model_id": 999}).status_code == 422
    assert len(probe.selections) == 3


def test_health_stub_and_override(client: TestClient) -> None:
    """REQ-LLM-01: the API reports unavailable without claiming adapter health."""
    login(client, "worker@example.com", "Analyst")
    assert client.get("/api/v1/llm/health").json() == {
        "status": "unavailable",
        "detail": "llama-server binary missing",
        "model": None,
    }

    class Provider:
        def check(self) -> LlmHealth:
            return LlmHealth(status="degraded", detail="warming up", model="local")

    client.app.state.llm_health_provider = Provider()
    assert client.get("/api/v1/llm/health").json() == {
        "status": "degraded",
        "detail": "warming up",
        "model": "local",
    }


def test_personal_settings(client: TestClient) -> None:
    """REQ-ING-04/05, REQ-IN-20: user-scope paths, disabled watches and preferences."""
    login(client, "worker@example.com", "Analyst")
    baseline = client.get("/api/v1/settings/me").json()
    assert baseline["resume_root_path"] is None and baseline["active_model_id"] is None
    assert baseline["watch"] == {
        "dl": {"enabled": False, "path": None, "pattern": "DL_*.xlsx"},
        "ct": {"enabled": False, "path": None, "pattern": "CT_*.xlsx"},
    }
    del client.headers["X-CSRF-Token"]
    assert client.put("/api/v1/settings/me", json={"page_size": 50}).status_code == 403
    login(client, "worker@example.com", "Analyst")
    values = {
        "resume_root_path": "  C:\\not-present\\resumes  ",
        "watch": {"dl": {"path": "  \\\\server\\dl  "}},
        "density": "compact",
        "page_size": 50,
        "default_landing": " demands ",
    }
    saved = client.put("/api/v1/settings/me", json=values)
    assert saved.status_code == 200, saved.text
    assert saved.json()["resume_root_path"] == values["resume_root_path"].strip()
    assert saved.json()["watch"]["dl"]["path"] == values["watch"]["dl"]["path"].strip()
    assert saved.json()["default_landing"] == "demands"
    assert client.put("/api/v1/settings/me", json={}).json() == saved.json()
    for body, code in [
        ({"watch": {"dl": {"enabled": True}}}, "PHASE_2_FEATURE"),
        ({"watch": {"ct": {"enabled": True}}}, "PHASE_2_FEATURE"),
        ({"active_model_id": 1}, "MODEL_NOT_APPROVED"),
        ({"page_size": 0}, "VALIDATION_ERROR"),
        ({"density": "tiny"}, "VALIDATION_ERROR"),
        ({"resume_root_path": "a" * 4097}, "VALIDATION_ERROR"),
        ({"watch": {"ct": {"path": "a" * 4097}}}, "VALIDATION_ERROR"),
        ({"default_landing": " "}, "VALIDATION_ERROR"),
        ({"admin_only": True}, "VALIDATION_ERROR"),
    ]:
        bad = client.put("/api/v1/settings/me", json=body)
        assert bad.status_code == 422, bad.text
        assert bad.json()["code"] == code
    assert client.get("/api/v1/settings/me").json() == saved.json()
    login(client)
    assert client.get("/api/v1/settings/me").json() == baseline
    client.put("/api/v1/occ/llm-models/1", json={"approved": True})
    login(client, "worker@example.com", "Analyst")
    updated = client.put("/api/v1/settings/me", json={"active_model_id": 1})
    assert updated.json()["active_model_id"] == 1
    assert client.get("/api/v1/llm/active").json()["model_id"] == 1
    reset = client.put(
        "/api/v1/settings/me", json={"resume_root_path": None, "active_model_id": None}
    )
    assert reset.json()["resume_root_path"] is None and reset.json()["active_model_id"] is None
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        entries = [
            json.loads(r[0])
            for r in db.execute("SELECT details_json FROM audit_log WHERE action='SETTING_CHANGE'")
        ]
        assert {"before": None, "after": {"value": 50}} in entries
        assert {"before": None, "after": {"value": values["resume_root_path"].strip()}} in entries
        assert db.execute("SELECT count(*) FROM settings WHERE scope='system'").fetchone()[0] == 0


@pytest.mark.parametrize("present", [True, False])
def test_startup_seeding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    """REQ-ELIG-02/03, REQ-LLM-06: startup reads PFC_CONFIG_DIR, missing files are optional."""
    config = tmp_path / "config"
    if present:
        config.mkdir()
        (config / "grades.yaml").write_text('["X1","X2"]')
        (config / "llm_models.approved.json").write_text(
            json.dumps(
                [
                    {
                        "name": "fixture",
                        "file_name": "fixture.gguf",
                        "file_sha256": "",
                        "size_mb": 0,
                        "ram_estimate_mb": 0,
                        "context_tokens": 1024,
                        "licence": "MIT",
                        "approved": 1,
                        "is_active": 1,
                    }
                ]
            )
        )
    monkeypatch.setenv("PFC_CONFIG_DIR", str(config))
    settings = Settings(
        data_dir=tmp_path / "data",
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    for _ in range(2):
        with TestClient(create_app(settings)) as result:
            assert result.get("/api/v1/health").status_code == 200
        with database(settings) as db:
            grades = [r["code"] for r in db.execute("SELECT * FROM grades ORDER BY ordinal")]
            assert grades == (["X1", "X2"] if present else [])
            models = [dict(r) for r in db.execute("SELECT * FROM llm_models")]
            assert len(models) == (1 if present else 2)
            if present:
                assert models[0]["name"] == "fixture" and models[0]["is_active"] == 1
            else:
                assert {r["name"] for r in models} == {
                    "Qwen3-4B-Instruct-2507",
                    "nomic-embed-text-v1.5",
                }
                assert all(r["file_sha256"] == "" and not r["approved"] for r in models)


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({"from": "2026-01-01T00:00:00.5Z"}, ["next"]),
        ({"from": "2026-01-01T00:00:00.100002Z"}, ["next", "second"]),
        ({"to": "2026-01-01T00:00:00.100001Z"}, ["first", "zero"]),
        ({"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}, ["zero"]),
        ({"from": "2026-01-01T00:00:00.100001Z", "to": "2026-01-01T00:00:00.100001Z"}, ["first"]),
        ({"from": "2026-01-01T01:00:00.100002+01:00"}, ["next", "second"]),
    ],
)
def test_audit_timestamp_precision(
    client: TestClient,
    bounds: dict[str, str],
    expected: list[str],
) -> None:
    """REQ-AUTH-05: inclusive audit ranges preserve precision and timezone equivalence."""
    login(client)
    with database(client.app.state.settings) as db:
        for entity_id, ts in [
            ("zero", "2026-01-01T00:00:00Z"),
            ("first", "2026-01-01T00:00:00.100001Z"),
            ("second", "2026-01-01T00:00:00.100002Z"),
            ("next", "2026-01-01T00:00:01Z"),
        ]:
            audit.append_audit(db, ts=ts, action="AUDIT_RANGE", entity_id=entity_id)
    params = {"action": "AUDIT_RANGE", **bounds}
    result = client.get("/api/v1/occ/audit-log", params=params).json()
    assert [row["entity_id"] for row in result["items"]] == expected
    assert result["total"] == len(expected) and result["chain_valid"]
    exported = client.get("/api/v1/occ/audit-log/export", params=params)
    assert [row["entity_id"] for row in csv.DictReader(io.StringIO(exported.text))] == expected


def test_internal_domains_preview_security(client: TestClient) -> None:
    """REQ-CLS-01, REQ-ROLE-04: preview requires Admin and CSRF despite being read-only."""
    path = "/api/v1/occ/internal-domains/preview"
    body = {"domains": ["example.com"]}
    assert client.post(path, json=body).status_code == 401
    login(client, "worker@example.com", "Analyst")
    assert client.post(path, json=body).status_code == 403
    login(client)
    del client.headers["X-CSRF-Token"]
    denied = client.post(path, json=body)
    assert denied.status_code == 403
    assert denied.headers["content-type"] == "application/problem+json"


def test_internal_domains_preview_read_only(client: TestClient) -> None:
    """REQ-CLS-01: preview preserves business data while refreshing the Admin session."""
    login(client)
    path = "/api/v1/occ/internal-domains/preview"
    assert client.post(path, json={"domains": []}).json() == {
        "total": 0,
        "internal": 0,
        "external": 0,
        "unknown": 0,
        "per_domain": [],
    }
    with database(client.app.state.settings) as db:
        for index, email in enumerate(
            [
                "a@example.com",
                " B@EXAMPLE.COM ",
                "sub@team.example.com",
                "x@other.test",
                None,
                " ",
                "x@notexample.com",
                "missing-at",
            ]
        ):
            db.execute(
                "INSERT INTO resources(short_name,email_address,short_name_key,email_key,"
                "eligibility,row_hash,created_at,updated_at) VALUES (?,?,?,?,'eligible',?,?,?)",
                (
                    str(index),
                    email,
                    str(index),
                    "unused",
                    str(index),
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
    clock = client.app.state.clock() + timedelta(seconds=2)
    client.app.state.clock = lambda: clock
    with database(client.app.state.settings) as db:
        before = [line for line in db.iterdump() if not line.startswith('INSERT INTO "sessions"')]
        audit_count = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        expiry = db.execute("SELECT expires_at FROM sessions WHERE user_id=1").fetchone()[0]
    response = client.post(path, json={"domains": [" @EXAMPLE.COM ", "other.test", "example.com"]})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total": 8,
        "internal": 3,
        "external": 3,
        "unknown": 2,
        "per_domain": [
            {"domain": "example.com", "matches": 2},
            {"domain": "other.test", "matches": 1},
        ],
    }
    empty = client.post(path, json={"domains": []}).json()
    assert (empty["internal"], empty["external"], empty["unknown"]) == (0, 6, 2)
    for domains in [["invalid"], ["example.com", "bad domain"], ["-example.com"]]:
        bad = client.post(path, json={"domains": domains})
        assert bad.status_code == 422 and bad.json()["code"] == "VALIDATION_ERROR"
    with database(client.app.state.settings) as db:
        assert [
            line for line in db.iterdump() if not line.startswith('INSERT INTO "sessions"')
        ] == before
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audit_count
        assert db.execute("SELECT expires_at FROM sessions WHERE user_id=1").fetchone()[0] > expiry
        assert audit.verify_chain(db)
