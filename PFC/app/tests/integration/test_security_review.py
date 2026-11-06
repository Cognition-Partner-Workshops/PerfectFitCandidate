"""P1-EX-08: REQ-AUTH-02..07 and REQ-OUT-03 security regression checks."""

import json
import logging
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
    assert response.json() == {
        "smtp.password": "[REDACTED]",
        "llm.api_key": "[REDACTED]",
        "provider.options": {"auth": [{"access_token": "[REDACTED]"}]},
    }
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
    options = {"options": [{"headers": {key: token}, "max_tokens": 64, "enabled": True}]}
    response = client.put("/api/v1/occ/settings", json={"provider.options": options})
    assert response.status_code == 200
    assert response.json()["provider.options"] == {
        "options": [{"headers": {key: "[REDACTED]"}, "max_tokens": 64, "enabled": True}]
    }
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
        expected = {
            "options": [{"headers": {key: "[REDACTED]"}, "max_tokens": 64, "enabled": True}]
        }
        assert json.loads(details[-1]) == {"before": expected, "after": expected}
        setting_audit = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE entity_id='provider.options'"
            ).fetchone()[0]
        )
        assert setting_audit["after"]["value"] == "[encrypted]"


def test_scoring_token_counts_remain_in_audit(client: TestClient) -> None:
    setup(client)
    login(client)
    with database(client.app.state.settings) as db:
        target = db.execute(
            "INSERT INTO scoring_templates(name,version,weights_json,rules_json,status,created_at) "
            "VALUES ('review',1,'{}',?,'draft',datetime('now'))",
            (json.dumps({"max_tokens": 128, "require_evidence": False}),),
        ).lastrowid
    response = client.put(
        f"/api/v1/occ/scoring-templates/{target}",
        json={
            "rules": {"max_tokens": 256, "require_evidence": True},
            "change_note": "Review",
        },
    )
    assert response.status_code == 200
    with database(client.app.state.settings) as db:
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE entity_type='scoring_templates' "
                "AND entity_id=?",
                (str(target),),
            ).fetchone()[0]
        )
        assert details["before"]["rules"] == {"max_tokens": 128, "require_evidence": False}
        assert details["after"]["rules"]["max_tokens"] == 256
        assert details["after"]["rules"]["require_evidence"] is True


def test_token_count_settings_remain_readable_and_audited(client: TestClient) -> None:
    setup(client)
    login(client)
    for limit in (128, 256):
        values = {"max_tokens": limit, "token_limit": limit, "num_tokens": limit}
        response = client.put("/api/v1/occ/settings", json={"llm.options": values})
        assert response.status_code == 200
        assert response.json()["llm.options"] == values
    with database(client.app.state.settings) as db:
        assert (
            json.loads(
                db.execute("SELECT value_json FROM settings WHERE key='llm.options'").fetchone()[0]
            )
            == values
        )
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE entity_id='llm.options' "
                "ORDER BY audit_id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert details["before"]["value"] == {
            "max_tokens": 128,
            "token_limit": 128,
            "num_tokens": 128,
        }
        assert details["after"]["value"] == values


def test_auth_tokens_encrypted_and_redacted(client: TestClient) -> None:
    setup(client)
    login(client)
    for token in ("FirstPrivateToken", "ReplacementPrivateToken"):
        response = client.put("/api/v1/occ/settings", json={"auth.tokens": token})
        assert response.status_code == 200
        assert response.json()["auth.tokens"] == "[REDACTED]"
        assert client.get("/api/v1/occ/settings").json()["auth.tokens"] == "[REDACTED]"
        with database(client.app.state.settings) as db:
            stored = db.execute(
                "SELECT value_json FROM settings WHERE key='auth.tokens'"
            ).fetchone()[0]
            assert token not in stored
            assert (
                json.loads(
                    SecretStore(client.app.state.settings.data_dir).decrypt(json.loads(stored))
                )
                == token
            )
            details = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT details_json FROM audit_log WHERE entity_id='auth.tokens' "
                    "ORDER BY audit_id"
                )
            ]
            assert details[-1]["after"] == {"value": "[encrypted]"}
            assert details[-1]["before"] == (
                None if len(details) == 1 else {"value": "[encrypted]"}
            )


def test_mixed_auth_headers_get_leaf_redaction_and_whole_audit_marker(client: TestClient) -> None:
    setup(client)
    login(client)
    options = {
        "headers": {"Authorization": "Bearer FirstPrivateToken", "X-Auth": "SecondPrivateToken"},
        "max_tokens": 64,
        "enabled": True,
    }
    response = client.put("/api/v1/occ/settings", json={"provider.options": options})
    expected = {
        "headers": {"Authorization": "[REDACTED]", "X-Auth": "[REDACTED]"},
        "max_tokens": 64,
        "enabled": True,
    }
    assert response.status_code == 200
    assert response.json()["provider.options"] == expected
    assert client.get("/api/v1/occ/settings").json()["provider.options"] == expected
    assert client.put("/api/v1/occ/settings", json=response.json()).json() == response.json()
    with database(client.app.state.settings) as db:
        stored = db.execute(
            "SELECT value_json FROM settings WHERE key='provider.options'"
        ).fetchone()[0]
        assert "FirstPrivateToken" not in stored and "SecondPrivateToken" not in stored
        assert (
            json.loads(SecretStore(client.app.state.settings.data_dir).decrypt(json.loads(stored)))
            == options
        )
        details = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT details_json FROM audit_log WHERE entity_id='provider.options'"
            )
        ]
        assert details == [{"before": None, "after": {"value": "[encrypted]"}}]


def test_previously_encrypted_setting_audit_is_conservative(client: TestClient) -> None:
    setup(client)
    login(client)
    assert (
        client.put(
            "/api/v1/occ/settings",
            json={
                "provider.options": {"Authorization": "PrivateToken"},
            },
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/v1/occ/settings",
            json={
                "provider.options": {"max_tokens": 64},
            },
        ).status_code
        == 200
    )
    with database(client.app.state.settings) as db:
        details = json.loads(
            db.execute(
                "SELECT details_json FROM audit_log WHERE entity_id='provider.options' "
                "ORDER BY audit_id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert details == {"before": {"value": "[encrypted]"}, "after": {"value": "[encrypted]"}}


def test_encrypted_setting_get_fails_closed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(client)
    login(client)
    assert (
        client.put(
            "/api/v1/occ/settings",
            json={
                "provider.options": {"X-Auth": "PrivateToken", "max_tokens": 64},
            },
        ).status_code
        == 200
    )

    def unavailable(self: SecretStore, value: str) -> bytes:
        raise SecretStoreError("unavailable")

    monkeypatch.setattr(SecretStore, "decrypt", unavailable)
    response = client.get("/api/v1/occ/settings")
    assert response.status_code == 200
    assert response.json()["provider.options"] == "[REDACTED]"
    assert "PrivateToken" not in response.text


def test_unclassified_legacy_ciphertext_stays_redacted(client: TestClient) -> None:
    setup(client)
    login(client)
    encrypted = SecretStore(client.app.state.settings.data_dir).encrypt(
        json.dumps("LegacyPrivateToken").encode()
    )
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO settings(scope,key,value_json,updated_at) "
            "VALUES ('system','tokenizer.options',?,datetime('now'))",
            (json.dumps(encrypted),),
        )
    response = client.get("/api/v1/occ/settings")
    assert response.json()["tokenizer.options"] == "[REDACTED]"
    assert "LegacyPrivateToken" not in response.text


def test_redacted_credentials_survive_metadata_edits_and_explicit_replacement(
    client: TestClient,
) -> None:
    setup(client)
    login(client)
    options = {
        "providers": [
            {"headers": {"Authorization": "FirstSecret"}, "enabled": True},
            {"auth": {"tokens": ["SecondSecret", "ThirdSecret"]}, "max_tokens": 64},
        ],
        "tokens": [None, ""],
    }
    response = client.put("/api/v1/occ/settings", json={"provider.options": options})
    assert response.status_code == 200
    updated = client.get("/api/v1/occ/settings").json()
    updated["provider.options"]["providers"][0]["enabled"] = False
    updated["provider.options"]["providers"][1]["max_tokens"] = 128
    assert client.put("/api/v1/occ/settings", json=updated).status_code == 200
    options["providers"][0]["enabled"] = False
    options["providers"][1]["max_tokens"] = 128
    with database(client.app.state.settings) as db:
        stored = json.loads(
            db.execute("SELECT value_json FROM settings WHERE key='provider.options'").fetchone()[0]
        )
        assert (
            json.loads(SecretStore(client.app.state.settings.data_dir).decrypt(stored)) == options
        )
    updated["provider.options"]["providers"][0]["headers"]["Authorization"] = "ReplacementSecret"
    assert client.put("/api/v1/occ/settings", json=updated).status_code == 200
    options["providers"][0]["headers"]["Authorization"] = "ReplacementSecret"
    with database(client.app.state.settings) as db:
        stored = json.loads(
            db.execute("SELECT value_json FROM settings WHERE key='provider.options'").fetchone()[0]
        )
        assert (
            json.loads(SecretStore(client.app.state.settings.data_dir).decrypt(stored)) == options
        )
        assert all(
            "ReplacementSecret" not in row[0]
            for row in db.execute(
                "SELECT details_json FROM audit_log WHERE details_json IS NOT NULL"
            )
        )


@pytest.mark.parametrize(
    "existing,incoming",
    [
        (None, {"token": "[REDACTED]"}),
        ({}, {"auth": {"tokens": ["[REDACTED]"]}}),
        ({"tokens": []}, {"tokens": ["[REDACTED]"]}),
        ({"tokens": ["FirstSecret"]}, {"tokens": ["[REDACTED]", "[REDACTED]"]}),
        ({"auth": []}, {"auth": {"token": "[REDACTED]"}}),
    ],
)
def test_redacted_credential_requires_existing_path(
    client: TestClient, existing: Any, incoming: Any
) -> None:
    setup(client)
    login(client)
    if existing is not None:
        assert (
            client.put("/api/v1/occ/settings", json={"provider.options": existing}).status_code
            == 200
        )
    with database(client.app.state.settings) as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM settings")]
        audits = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    response = client.put(
        "/api/v1/occ/settings",
        json={
            "matching.top_n": 77,
            "provider.options": incoming,
        },
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    with database(client.app.state.settings) as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM settings")] == before
        assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits


def test_marker_restore_requires_decryption_but_explicit_replacement_does_not(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(client)
    login(client)
    assert (
        client.put(
            "/api/v1/occ/settings",
            json={
                "provider.options": {"token": "OriginalSecret", "enabled": True},
            },
        ).status_code
        == 200
    )

    def unavailable(self: SecretStore, value: str) -> bytes:
        raise SecretStoreError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(SecretStore, "decrypt", unavailable)
        response = client.put(
            "/api/v1/occ/settings",
            json={
                "provider.options": {"token": "[REDACTED]", "enabled": False},
            },
        )
        assert response.status_code == 503
        assert response.json()["code"] == "SECRET_STORE_UNAVAILABLE"
        assert (
            client.put(
                "/api/v1/occ/settings",
                json={
                    "provider.options": {"token": "ReplacementSecret", "enabled": False},
                },
            ).status_code
            == 200
        )
    with database(client.app.state.settings) as db:
        stored = json.loads(
            db.execute("SELECT value_json FROM settings WHERE key='provider.options'").fetchone()[0]
        )
        assert json.loads(SecretStore(client.app.state.settings.data_dir).decrypt(stored)) == {
            "token": "ReplacementSecret",
            "enabled": False,
        }


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
