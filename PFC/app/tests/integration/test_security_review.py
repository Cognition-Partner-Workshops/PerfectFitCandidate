"""P1-EX-08: REQ-AUTH-02..07 and REQ-OUT-03 security regression checks."""

import json
import logging
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import Type
from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.passwords import hasher
from pfc.security.secrets_store import SecretStore, SecretStoreError
from pfc.security.setup import code_path
from pfc.services.occ.common import audit_write

EMAIL = "reviewer@example.com"
PASSWORD = "Strong!Random4567"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        frontend_dir=tmp_path,
        config_dir=tmp_path,
        allowed_hosts=["testserver"],
        llm_enabled=False,
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings)) as result:
        yield result


def setup(client: TestClient) -> None:
    response = client.post(
        "/api/v1/setup/admin",
        json={
            "setup_code": code_path(client.app.state.settings).read_text(),
            "email": EMAIL,
            "display_name": "Reviewer",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201


def login(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "role": "Admin",
        },
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def http_operations(client: TestClient) -> list[tuple[str, str]]:
    operations = []
    for route in client.app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        for effective in contexts() if contexts is not None else [route]:
            path = getattr(effective, "path", "")
            methods = getattr(effective, "methods", None)
            if path and methods:
                operations.extend(
                    (method, re.sub(r"\{[^}]+\}", "999999", path)) for method in methods
                )
    return sorted(operations)


def test_all_api_writes_require_csrf(client: TestClient) -> None:
    """REQ-AUTH-04/07: no authenticated API write exemptions, including login/setup."""
    setup(client)
    login(client)
    del client.headers["X-CSRF-Token"]
    operations = [
        (method, path)
        for method, path in http_operations(client)
        if path.startswith("/api/v1/") and method in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert operations
    for method, path in operations:
        response = client.request(method, path, json={})
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["code"] == "CSRF_INVALID", (method, path)
        assert response.headers["content-type"] == "application/problem+json"


def test_must_change_covers_every_api_route(client: TestClient) -> None:
    """REQ-AUTH-02: enumerate every HTTP operation with the exact recovery allowlist."""
    setup(client)
    login(client)
    with database(client.app.state.settings) as db:
        db.execute("UPDATE users SET must_change_password=1")
    allowed = {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/setup/status"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/password"),
        ("POST", "/api/v1/auth/logout"),
    }
    operations = http_operations(client)
    assert allowed <= set(operations)
    for method, path in operations:
        if (method, path) not in allowed:
            response = client.request(method, path, json={})
            assert response.status_code == 403, (method, path, response.text)
            assert response.headers["content-type"] == "application/problem+json"
            if method != "HEAD":
                assert response.json()["code"] == "PASSWORD_CHANGE_REQUIRED", (method, path)
    for method, path in sorted(allowed - {("POST", "/api/v1/auth/logout")}):
        response = client.request(method, path, json={})
        assert response.status_code == (422 if method == "POST" else 200)
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_auth_defaults_and_cookie_rotation(client: TestClient) -> None:
    """REQ-AUTH-04: production parameters, cookie flags and login/password rotation."""
    defaults = Settings.model_fields
    assert [
        defaults[name].default
        for name in (
            "argon_memory_cost",
            "argon_time_cost",
            "argon_parallelism",
            "lockout_threshold",
            "lockout_minutes",
            "idle_timeout_min",
            "absolute_timeout_h",
        )
    ] == [65536, 3, 4, 5, 15, 60, 12]
    assert hasher(client.app.state.settings).type is Type.ID
    setup(client)
    login(client)
    first = client.cookies.get("pfc_session")
    login(client)
    second = client.cookies.get("pfc_session")
    assert first != second
    cookie = next(c for c in client.cookies.jar if c.name == "pfc_session")
    assert cookie.path == "/" and not cookie.secure
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.get_nonstandard_attr("SameSite") == "strict"
    response = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": PASSWORD,
            "new_password": "Another!Random5678",
        },
    )
    assert response.status_code == 200
    assert client.cookies.get("pfc_session") not in {first, second}
    for old in (first, second):
        client.cookies.clear()
        client.cookies.set("pfc_session", old)
        assert client.get("/api/v1/auth/me").status_code == 401


def test_auth_setup_unmask_logs_omit_pii(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-AUTH-05/06: exercise real successful/failed login, bootstrap and unmask paths."""
    caplog.set_level(logging.DEBUG)
    code = code_path(client.app.state.settings).read_text()
    setup(client)
    failed_password = "Wrong!Sensitive123"
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": EMAIL,
            "password": failed_password,
            "role": "Admin",
        },
    )
    assert response.status_code == 401
    login(client)
    assert client.post(
        "/api/v1/pii/unmask",
        json={
            "entity": "occ_user",
            "id": 1,
            "field": "email",
        },
    ).json() == {"value": EMAIL}
    with database(client.app.state.settings) as db:
        actions = {r[0] for r in db.execute("SELECT action FROM audit_log")}
    assert {"SETUP_ADMIN", "LOGIN_FAIL", "LOGIN_OK", "UNMASK"} <= actions
    for sensitive in (EMAIL, PASSWORD, failed_password, code):
        assert sensitive not in caplog.text


def test_job_exception_payload_not_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-AUTH-06: untrusted extraction exceptions must not leak source PII to logs."""
    started = threading.Event()

    def fail(context: object) -> None:
        started.set()
        raise ValueError(f"{EMAIL} {PASSWORD}")

    runner = client.app.state.job_runner
    job_id = runner.submit("security-review", fail)
    assert started.wait(5)
    runner.shutdown()
    assert runner.get(job_id).status == "failed"
    assert "Background job" in caplog.text
    assert EMAIL not in caplog.text and PASSWORD not in caplog.text


def test_secret_settings_encrypted_and_write_only(client: TestClient) -> None:
    """REQ-AUTH-06: the existing generic settings writer must not persist/return credentials."""
    setup(client)
    login(client)
    secrets = {
        "smtp.password": "SensitiveSMTPPassword",
        "llm.api_key": "SensitiveProviderKey",
        "provider.options": {"auth": [{"access_token": "SensitiveNestedToken"}]},
    }
    response = client.put("/api/v1/occ/settings", json=secrets)
    assert response.status_code == 200
    assert response.json() == {key: "[REDACTED]" for key in secrets}
    assert client.get("/api/v1/occ/settings").json() == response.json()
    with database(client.app.state.settings) as db:
        for key, value in secrets.items():
            row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            encrypted = json.loads(row["value_json"])
            assert (
                json.loads(SecretStore(client.app.state.settings.data_dir).decrypt(encrypted))
                == value
            )
            details = db.execute("SELECT details_json FROM audit_log").fetchall()
            for secret in ("SensitiveSMTPPassword", "SensitiveProviderKey", "SensitiveNestedToken"):
                assert secret not in row["value_json"]
                assert all(secret not in r[0] for r in details if r[0])
        before = db.execute("SELECT key,value_json FROM settings ORDER BY key").fetchall()
    assert client.put("/api/v1/occ/settings", json=response.json()).status_code == 200
    with database(client.app.state.settings) as db:
        assert [tuple(r) for r in before] == [
            tuple(r) for r in db.execute("SELECT key,value_json FROM settings ORDER BY key")
        ]


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "BEARER",
        "Cookie",
        "Set-Cookie",
        "credential",
        "api_key",
        "apiKey",
        "client_secret",
        "private-key",
        "Proxy-Authorization",
        "session_key",
    ],
)
def test_nested_header_credentials_encrypted_and_audit_redacted(
    client: TestClient,
    key: str,
) -> None:
    setup(client)
    login(client)
    token = "Bearer SensitiveReviewToken"
    options = {"options": [{"headers": {key: token}}]}
    response = client.put("/api/v1/occ/settings", json={"provider.options": options})
    assert response.status_code == 200
    assert response.json()["provider.options"] == "[REDACTED]"
    with database(client.app.state.settings) as db:
        stored = db.execute(
            "SELECT value_json FROM settings WHERE key='provider.options'"
        ).fetchone()[0]
        assert token not in stored
        assert (
            json.loads(SecretStore(client.app.state.settings.data_dir).decrypt(json.loads(stored)))
            == options
        )
        audit_write(
            db,
            1,
            "SETTING_CHANGE",
            "settings",
            "direct-header-review",
            options,
            options,
        )
        details = [r[0] for r in db.execute("SELECT details_json FROM audit_log") if r[0]]
        assert all(token not in value for value in details)
        assert "[REDACTED]" in details[-1]


def test_secret_setting_failure_is_atomic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(client)
    login(client)
    before = client.get("/api/v1/occ/settings").json()
    assert client.put("/api/v1/occ/settings", json={}).json() == before
    with database(client.app.state.settings) as db:
        audits_before = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    def unavailable(self: SecretStore, value: bytes) -> str:
        raise SecretStoreError("unavailable")

    monkeypatch.setattr(SecretStore, "encrypt", unavailable)
    response = client.put(
        "/api/v1/occ/settings",
        json={
            "matching.top_n": 10,
            "smtp.password": "do-not-store",
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "SECRET_STORE_UNAVAILABLE"
    assert client.get("/api/v1/occ/settings").json() == before
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits_before


def test_legacy_secret_setting_hidden_and_upgraded_on_resave(client: TestClient) -> None:
    setup(client)
    login(client)
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO settings(scope,key,value_json,updated_at) "
            "VALUES ('system','smtp.password',?,datetime('now'))",
            (json.dumps("LegacyCredential"),),
        )
    assert client.get("/api/v1/occ/settings").json()["smtp.password"] == "[REDACTED]"
    assert (
        client.put("/api/v1/occ/settings", json={"smtp.password": "LegacyCredential"}).status_code
        == 200
    )
    with database(client.app.state.settings) as db:
        assert (
            "LegacyCredential"
            not in db.execute(
                "SELECT value_json FROM settings WHERE key='smtp.password'"
            ).fetchone()[0]
        )


def test_seed_logs_do_not_echo_profile_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    settings = Settings(
        data_dir=tmp_path,
        config_dir=tmp_path / EMAIL,
        frontend_dir=tmp_path,
        allowed_hosts=["testserver"],
        llm_enabled=False,
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings)):
        assert "Skipping missing seed file" in caplog.text
        assert EMAIL not in caplog.text
        assert str(tmp_path) not in caplog.text
