import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pfc.config import Settings
from pfc.db import audit, users
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.dependencies import require_role
from pfc.security.passwords import hasher, validate_password
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.security.setup import code_path

PASSWORD = "Strong!Random4567"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        bundled_models_dir=tmp_path / "bundled-models",
        frontend_dir=tmp_path,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    app = create_app(settings)
    app.state.now = datetime(2026, 1, 1, tzinfo=UTC)
    app.state.clock = lambda: app.state.now

    @app.post("/admin", dependencies=[Depends(require_role("admin"))])
    def admin() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/analyst", dependencies=[Depends(require_role("Analyst"))])
    def analyst() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret failure")

    @app.get("/http-error")
    def http_error() -> None:
        raise HTTPException(418, "Teapot")

    with TestClient(app, raise_server_exceptions=False) as result:
        yield result


def setup(client: TestClient) -> None:
    settings = client.app.state.settings
    code = code_path(settings).read_text()
    result = client.post(
        "/api/v1/setup/admin",
        json={
            "setup_code": code,
            "email": "owner@example.com",
            "display_name": "Owner",
            "password": PASSWORD,
        },
    )
    assert result.status_code == 201, result.text


def login(
    client: TestClient,
    role: str = "Admin",
    email: str = "owner@example.com",
    password: str = PASSWORD,
) -> str:
    client.cookies.clear()
    result = client.post(
        "/api/v1/auth/login",
        json={
            "role": role,
            "email": email,
            "password": password,
        },
    )
    assert result.status_code == 200, result.text
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert "Path=/" in result.headers["set-cookie"]
    return result.json()["csrf_token"]


def test_public_setup_status(client: TestClient) -> None:
    """REQ-AUTH-03: bootstrap status exposes only an Admin-exists boolean."""
    response = client.get("/api/v1/setup/status")
    assert response.status_code == 200
    assert response.json() == {"admin_exists": False}
    assert set(response.json()) == {"admin_exists"}
    setup(client)
    assert not client.cookies
    response = client.get("/api/v1/setup/status")
    assert response.status_code == 200
    assert response.json() == {"admin_exists": True}
    assert set(response.json()) == {"admin_exists"}
    with database(client.app.state.settings) as db:
        db.execute("UPDATE users SET must_change_password=1")
    login(client)
    assert client.get("/api/v1/setup/status").json() == {"admin_exists": True}
    client.cookies.clear()
    assert client.get("/api/v1/setup/status").json() == {"admin_exists": True}


def test_setup_login_logout(client: TestClient) -> None:
    body = {
        "setup_code": "bad",
        "email": "owner@example.com",
        "display_name": "Owner",
        "password": PASSWORD,
    }
    assert client.post("/api/v1/setup/admin", json=body).status_code == 403
    body["setup_code"] = code_path(client.app.state.settings).read_text()
    body["password"] = "short"
    assert client.post("/api/v1/setup/admin", json=body).status_code == 422
    setup(client)
    assert not code_path(client.app.state.settings).exists()
    assert client.post("/api/v1/setup/admin", json=body).status_code == 409
    csrf = login(client)
    cookie = client.cookies.get("pfc_session")
    assert cookie and len(cookie) >= 43
    assert client.post("/admin").status_code == 403
    assert client.post("/admin", headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert client.post("/admin", headers={"X-CSRF-Token": csrf}).status_code == 200
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["csrf_token"] == csrf
    assert client.get("/api/v1/auth/me").json()["csrf_token"] == csrf
    csrf = me.json()["csrf_token"]
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    client.cookies.set("pfc_session", cookie)
    assert client.get("/api/v1/auth/me").status_code == 401
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        actions = {row[0] for row in db.execute("SELECT action FROM audit_log")}
        assert {"SETUP_ADMIN", "LOGIN_OK", "LOGOUT"} <= actions


def test_lockout(client: TestClient) -> None:
    setup(client)
    for _ in range(5):
        result = client.post(
            "/api/v1/auth/login",
            json={"role": "Admin", "email": "owner@example.com", "password": "wrong"},
        )
        assert result.status_code == 401
        assert result.json()["detail"] == "invalid credentials"
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"role": "Admin", "email": "owner@example.com", "password": PASSWORD},
        ).status_code
        == 401
    )
    client.app.state.now += timedelta(minutes=16)
    login(client)
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        assert (
            db.execute("SELECT count(*) FROM audit_log WHERE action='LOCKOUT'").fetchone()[0] == 1
        )
        assert users.get_user_by_email(db, "owner@example.com").failed_attempts == 0


def test_role_and_password_change(client: TestClient) -> None:
    setup(client)
    with database(client.app.state.settings) as db:
        users.create_user(
            db,
            email="worker@example.com",
            display_name="Worker",
            role_id=2,
            password_hash=hasher(client.app.state.settings).hash("Initial12345"),
            now=stamp(client.app.state.now),
        )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"role": "Admin", "email": "worker@example.com", "password": "Initial12345"},
        ).status_code
        == 401
    )
    csrf = login(client, "Analyst", "worker@example.com", "Initial12345")
    assert (
        client.post("/analyst", headers={"X-CSRF-Token": csrf}).json()["code"]
        == "PASSWORD_CHANGE_REQUIRED"
    )
    assert client.get("/api/v1/auth/me").json()["must_change_password"]
    csrf = client.get("/api/v1/auth/me").json()["csrf_token"]
    for current, new, status in [
        ("wrong", "NewRandom12345", 401),
        ("Initial12345", "password1234", 422),
        ("Initial12345", "NewRandom12345", 200),
    ]:
        result = client.post(
            "/api/v1/auth/password",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": current, "new_password": new},
        )
        assert result.status_code == status, result.text
    csrf = result.json()["csrf_token"]
    assert not result.json()["must_change_password"]
    assert client.post("/analyst", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.post("/admin", headers={"X-CSRF-Token": csrf}).status_code == 403
    csrf = login(client, "Analyst")
    assert client.post("/admin", headers={"X-CSRF-Token": csrf}).status_code == 403
    assert client.post("/analyst", headers={"X-CSRF-Token": csrf}).status_code == 200
    with database(client.app.state.settings) as db:
        assert audit.verify_chain(db)
        assert db.execute("SELECT 1 FROM audit_log WHERE action='PASSWORD_CHANGE'").fetchone()


def test_expiry(client: TestClient) -> None:
    setup(client)
    login(client)
    client.app.state.now += timedelta(minutes=61)
    assert client.get("/api/v1/auth/me").status_code == 401
    login(client)
    for _ in range(23):
        client.app.state.now += timedelta(minutes=30)
        assert client.get("/api/v1/auth/me").status_code == 200
    client.app.state.now += timedelta(minutes=30)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_transport_and_problems(client: TestClient) -> None:
    assert client.get("/api/v1/health", headers={"Host": "evil.com"}).status_code == 400
    assert client.get("/api/v1/health", headers={"Origin": "http://evil.com"}).status_code == 403
    result = client.get(
        "/api/v1/health", headers={"Origin": "http://testserver", "X-Request-ID": "trace-123"}
    )
    assert result.status_code == 200
    for key, value in {
        "Content-Security-Policy": "default-src 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "X-Request-ID": "trace-123",
    }.items():
        assert result.headers[key] == value
    for method, path, status in [
        ("GET", "/boom", 500),
        ("GET", "/http-error", 418),
        ("GET", "/missing", 404),
        ("POST", "/api/v1/health", 405),
    ]:
        result = client.request(method, path)
        assert result.status_code == status
        assert result.headers["content-type"] == "application/problem+json"
        assert result.json()["request_id"] == result.headers["X-Request-ID"]
        assert "secret failure" not in result.text
    result = client.post("/api/v1/auth/login", json={})
    assert result.status_code == 422
    assert result.headers["content-type"] == "application/problem+json"
    assert result.json()["request_id"] == result.headers["X-Request-ID"]
    assert result.json()["code"] == "VALIDATION_ERROR"
    with pytest.raises(ValidationError):
        Settings(host="0.0.0.0")


def test_policy_and_rehash(client: TestClient) -> None:
    for password in ["short", "aaaaaaaaaaaaaaaa", "OwnerStrong45678!", "password123456!"]:
        with pytest.raises(Problem):
            validate_password(password, "owner@example.com", "Admin")
    with pytest.raises(Problem):
        validate_password("password1234", "worker@example.com", "Analyst")
    validate_password("longsimpleword", "worker@example.com", "Analyst")
    setup(client)
    settings = client.app.state.settings
    with database(settings) as db:
        old_hash = users.get_user_by_email(db, "owner@example.com").password_hash
    settings.argon_time_cost = 2
    login(client)
    with database(settings) as db:
        new_hash = users.get_user_by_email(db, "owner@example.com").password_hash
        assert new_hash != old_hash
        assert not hasher(settings).check_needs_rehash(new_hash)


def test_jobs_endpoints(client: TestClient) -> None:
    for path in ["/api/v1/jobs/1", "/api/v1/jobs/1/events"]:
        assert client.get(path).status_code == 401
    assert client.post("/api/v1/jobs/1/cancel").status_code == 401
    setup(client)
    csrf = login(client)
    for path in ["/api/v1/jobs/99999", "/api/v1/jobs/99999/events"]:
        result = client.get(path)
        assert result.status_code == 404
        assert result.json()["code"] == "JOB_NOT_FOUND"
    assert (
        client.post("/api/v1/jobs/99999/cancel", headers={"X-CSRF-Token": csrf}).status_code == 404
    )
    runner = client.app.state.job_runner
    job_id = runner.submit("test", lambda context: context.report_progress(0.5, "working"))
    result = client.get(f"/api/v1/jobs/{job_id}/events")
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in result.text
    assert '"status": "completed"' in result.text
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"
    started = threading.Event()

    def cancellable(context):
        started.set()
        context.cancelled.wait(5)
        context.raise_if_cancelled()

    active = runner.submit("cancel", cancellable)
    assert started.wait(5)
    assert client.post(f"/api/v1/jobs/{active}/cancel").status_code == 403
    assert (
        client.post(f"/api/v1/jobs/{active}/cancel", headers={"X-CSRF-Token": csrf}).status_code
        == 200
    )
    assert '"status": "cancelled"' in client.get(f"/api/v1/jobs/{active}/events").text
    csrf = login(client, "Analyst")
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert (
        client.post(f"/api/v1/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf}).status_code
        == 200
    )
