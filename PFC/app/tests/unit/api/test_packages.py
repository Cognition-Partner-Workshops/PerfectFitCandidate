"""Package HTTP security, confirmation, history and OTP login flow (REQ-OCC-01..03)."""

import base64
import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pfc.api.routers import packages
from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.users import create_user
from pfc.main import create_app
from pfc.security import package_signing as signing
from pfc.security.passwords import hasher
from pfc.services.packages import staging
from pfc.services.packages.importer import SettingsImporter
from pfc.services.packages.keys import SigningKeys

PREFIX = "/api/v1"
PASSWORD = "Strong!Random4567"
PASSPHRASE = "test signing passphrase"


@pytest.fixture
def apps(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    clients: list[TestClient] = []
    for name in ("admin", "analyst"):
        settings = Settings(
            data_dir=tmp_path / name,
            allowed_hosts=["testserver"],
            argon_memory_cost=1024,
            argon_time_cost=1,
            argon_parallelism=1,
        )
        app = create_app(settings)
        app.include_router(packages.router, prefix=PREFIX)
        clients.append(TestClient(app, raise_server_exceptions=False))
    with clients[0] as source, clients[1] as target:
        for client in (source, target):
            settings = client.app.state.settings
            now = datetime.now(UTC).isoformat()
            with database(settings) as db:
                for email, role_id, display in [
                    ("owner@example.com", 1, "Owner"),
                    ("worker@example.com", 2, "Worker"),
                ]:
                    create_user(
                        db,
                        email=email,
                        display_name=display,
                        role_id=role_id,
                        password_hash=hasher(settings).hash(PASSWORD),
                        now=now,
                        must_change_password=False,
                    )
                if client is target:
                    create_user(
                        db,
                        email="installer@example.com",
                        display_name="Installer",
                        role_id=2,
                        password_hash=hasher(settings).hash(PASSWORD),
                        now=now,
                        must_change_password=False,
                    )
        with database(source.app.state.settings) as db:
            db.execute("INSERT INTO skills VALUES (1, 'Python', NULL, 'admin', 1)")
        yield source, target


def login(
    client: TestClient,
    *,
    role: str = "Admin",
    email: str = "owner@example.com",
    password: str = PASSWORD,
) -> str:
    client.cookies.clear()
    result = client.post(
        f"{PREFIX}/auth/login", json={"role": role, "email": email, "password": password}
    )
    assert result.status_code == 200, result.text
    return str(result.json()["csrf_token"])


def export(client: TestClient, csrf: str, **options: Any) -> httpx.Response:
    return client.post(
        f"{PREFIX}/occ/settings-packages/export",
        headers={"X-CSRF-Token": csrf},
        json={"passphrase": PASSPHRASE, **options},
    )


def provision(source: TestClient, target: TestClient) -> None:
    result = source.get(f"{PREFIX}/occ/settings-packages/signing-key")
    assert result.status_code == 200, result.text
    public = base64.b64decode(result.json()["public_key_base64"])
    assert hashlib.sha256(public).hexdigest() == result.json()["key_id"]
    directory = target.app.state.settings.data_dir / "keys/trusted"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "admin.pub").write_bytes(public)


def preview(
    client: TestClient, csrf: str, package: bytes, *, model: bool = False
) -> httpx.Response:
    endpoint = "/models/addons/preview" if model else "/settings-packages/preview"
    return client.post(
        PREFIX + endpoint,
        headers={"X-CSRF-Token": csrf},
        files={"file": ("model.pfcmodel" if model else "settings.pfcpkg", package)},
    )


def apply(client: TestClient, csrf: str, token: str, *, model: bool = False) -> httpx.Response:
    endpoint = "/models/addons/apply" if model else "/settings-packages/apply"
    return client.post(PREFIX + endpoint, headers={"X-CSRF-Token": csrf}, json={"token": token})


def history(client: TestClient) -> list[dict[str, Any]]:
    result = client.get(f"{PREFIX}/settings/settings-package/history")
    assert result.status_code == 200, result.text
    return list(result.json())


def model_package(source: TestClient) -> bytes:
    output = io.BytesIO()
    signing.build_model_package(
        {
            "model.onnx": b"model weights",
            "model.v1.json": signing.canonical_json(
                {
                    "model_type": "embedding",
                    "name": "nomic",
                    "file": "model.onnx",
                    "runtime": "onnx",
                    "licence": "Apache-2.0",
                }
            ),
        },
        output,
        private=SigningKeys(source.app.state.settings).get_or_create(PASSPHRASE),
        package_version=1,
        min_app_version="0.1.0",
        max_app_version="0.1.0",
        created_at=datetime.now(UTC).isoformat(),
    )
    return output.getvalue()


def problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code
    assert response.json()["request_id"]


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/occ/settings-packages/export", {"passphrase": PASSPHRASE}),
        ("GET", "/occ/settings-packages/signing-key", None),
        ("POST", "/settings-packages/preview", None),
        ("POST", "/settings-packages/apply", {"token": "a" * 43}),
        ("POST", "/settings-packages/rollback", None),
        ("GET", "/settings-packages/pending", None),
        ("GET", "/settings/settings-package/history", None),
        ("POST", "/models/addons/preview", None),
        ("POST", "/models/addons/apply", {"token": "a" * 43}),
    ],
)
def test_unauthenticated(
    apps: tuple[TestClient, TestClient], method: str, path: str, body: dict[str, Any] | None
) -> None:
    _, target = apps
    problem(target.request(method, PREFIX + path, json=body), 401, "AUTH_REQUIRED")


@pytest.mark.parametrize(
    "path, body",
    [
        ("/occ/settings-packages/export", {"passphrase": PASSPHRASE}),
        ("/settings-packages/preview", None),
        ("/settings-packages/apply", {"token": "a" * 43}),
        ("/settings-packages/rollback", None),
        ("/models/addons/preview", None),
        ("/models/addons/apply", {"token": "a" * 43}),
    ],
)
def test_csrf(apps: tuple[TestClient, TestClient], path: str, body: dict[str, Any] | None) -> None:
    source, _ = apps
    login(source)
    problem(source.post(PREFIX + path, json=body), 403, "CSRF_INVALID")


def test_analyst_rbac_and_empty_states(apps: tuple[TestClient, TestClient]) -> None:
    source, target = apps
    csrf = login(target, role="Analyst", email="installer@example.com")
    problem(export(target, csrf), 403, "FORBIDDEN")
    problem(target.get(f"{PREFIX}/occ/settings-packages/signing-key"), 403, "FORBIDDEN")
    problem(preview(target, csrf, b"model", model=True), 403, "FORBIDDEN")
    problem(apply(target, csrf, "a" * 43, model=True), 403, "FORBIDDEN")
    assert history(target) == []
    assert target.get(f"{PREFIX}/settings-packages/pending").json() == []
    problem(
        target.post(f"{PREFIX}/settings-packages/rollback", headers={"X-CSRF-Token": csrf}),
        409,
        "PACKAGE_NO_PREVIOUS_VERSION",
    )
    login(source)
    problem(
        source.get(f"{PREFIX}/occ/settings-packages/signing-key"), 404, "SIGNING_KEY_UNAVAILABLE"
    )
    csrf = login(source, role="Analyst")
    problem(export(source, csrf), 403, "FORBIDDEN")


def test_otp_roundtrip_password_change_and_no_retrieval(
    apps: tuple[TestClient, TestClient],
) -> None:
    source, target = apps
    source_csrf = login(source)
    result = export(source, source_csrf)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["file_name"] == "PFC-settings-v1.pfcpkg"
    package = base64.b64decode(result.json()["content_base64"])
    otp = result.json()["one_time_passwords"][0]["otp"]
    provision(source, target)
    csrf = login(target, role="Analyst", email="installer@example.com")
    before = preview(target, csrf, package)
    assert before.status_code == 200, before.text
    assert before.json()["diff"]["skills"]["added"] == ["1"]
    assert history(target) == []
    confirmed = apply(target, csrf, before.json()["token"])
    assert confirmed.status_code == 200 and confirmed.json() == {"version": 1}
    problem(apply(target, csrf, before.json()["token"]), 404, "PACKAGE_PREVIEW_NOT_FOUND")
    csrf = login(target, role="Analyst", email="worker@example.com", password=otp)
    problem(
        target.get(f"{PREFIX}/settings/settings-package/history"), 403, "PASSWORD_CHANGE_REQUIRED"
    )
    changed = target.post(
        f"{PREFIX}/auth/password",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": otp, "new_password": "NewRandom12345"},
    )
    assert changed.status_code == 200, changed.text
    assert not changed.json()["must_change_password"]
    assert history(target)[0]["is_current"] is True
    assert "one_time_passwords" not in str(history(target))
    assert "otp" not in source.get(f"{PREFIX}/occ/settings-packages/signing-key").json()
    next_export = export(source, source_csrf)
    assert next_export.status_code == 200
    assert next_export.json()["version"] == 2
    assert next_export.json()["one_time_passwords"][0]["otp"] != otp
    for client in (source, target):
        for path in client.app.state.settings.data_dir.rglob("*"):
            if path.is_file():
                assert otp.encode() not in path.read_bytes(), path


def test_rollback_does_not_restore_used_otp(apps: tuple[TestClient, TestClient]) -> None:
    source, target = apps
    source_csrf, csrf = login(source), login(target)
    first = export(source, source_csrf).json()
    provision(source, target)
    token = preview(target, csrf, base64.b64decode(first["content_base64"])).json()["token"]
    assert apply(target, csrf, token).status_code == 200
    second = export(source, source_csrf).json()
    token = preview(target, csrf, base64.b64decode(second["content_base64"])).json()["token"]
    assert apply(target, csrf, token).status_code == 200
    otp = second["one_time_passwords"][0]["otp"]
    csrf = login(target, role="Analyst", email="worker@example.com", password=otp)
    response = target.post(
        f"{PREFIX}/auth/password",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": otp, "new_password": "NewRandom12345"},
    )
    assert response.status_code == 200, response.text
    csrf = login(target)
    assert (
        target.post(
            f"{PREFIX}/settings-packages/rollback", headers={"X-CSRF-Token": csrf}
        ).status_code
        == 200
    )
    for old in (otp, first["one_time_passwords"][0]["otp"]):
        response = target.post(
            f"{PREFIX}/auth/login",
            json={"role": "Analyst", "email": "worker@example.com", "password": old},
        )
        assert response.status_code == 401, response.text
    login(target, role="Analyst", email="worker@example.com", password="NewRandom12345")
    assert target.get(f"{PREFIX}/settings/settings-package/history").status_code == 200


@pytest.mark.parametrize(
    "failure, status, code",
    [
        ("signature", 422, "PACKAGE_INVALID_SIGNATURE"),
        ("stale", 409, "PACKAGE_STALE_VERSION"),
        ("wrong_kind", 422, "PACKAGE_WRONG_KIND"),
        ("incompatible", 409, "PACKAGE_INCOMPATIBLE_APP"),
    ],
)
def test_package_problem_codes(
    apps: tuple[TestClient, TestClient], failure: str, status: int, code: str
) -> None:
    source, target = apps
    source_csrf = login(source)
    response = export(source, source_csrf)
    package = base64.b64decode(response.json()["content_base64"])
    provision(source, target)
    csrf = login(target)
    if failure == "signature":
        output = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(package)) as archive,
            zipfile.ZipFile(output, "w") as result,
        ):
            for name in archive.namelist():
                result.writestr(
                    name, bytes(64) if name == signing.SIGNATURE_NAME else archive.read(name)
                )
        package = output.getvalue()
    elif failure == "stale":
        token = preview(target, csrf, package).json()["token"]
        assert apply(target, csrf, token).status_code == 200
    elif failure == "wrong_kind":
        package = model_package(source)
    else:
        package = base64.b64decode(
            export(source, source_csrf, min_app_version="9.0", max_app_version="9.0").json()[
                "content_base64"
            ]
        )
    problem(preview(target, csrf, package), status, code)
    assert not list((target.app.state.settings.data_dir / "packages/staged").glob("*.upload"))


def test_atomic_api_apply_rollback_and_history(
    apps: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = apps
    source_csrf, csrf = login(source), login(target)
    first = export(source, source_csrf)
    provision(source, target)
    token = preview(target, csrf, base64.b64decode(first.json()["content_base64"])).json()["token"]
    assert apply(target, csrf, token).status_code == 200
    with database(source.app.state.settings) as db:
        db.execute("UPDATE skills SET name='Rust'")
    second = export(source, source_csrf)
    token = preview(target, csrf, base64.b64decode(second.json()["content_base64"])).json()["token"]

    def fail() -> None:
        raise RuntimeError("sensitive mid-apply failure details")

    def importer(settings: Settings) -> SettingsImporter:
        return SettingsImporter(settings, failure_hook=fail)

    with monkeypatch.context() as patch:
        patch.setattr(packages, "SettingsImporter", importer)
        response = apply(target, csrf, token)
        problem(response, 500, "PACKAGE_OPERATION_FAILED")
        assert "sensitive" not in response.text
    assert [row["version"] for row in history(target)] == [1]
    with database(target.app.state.settings) as db:
        assert db.execute("SELECT name FROM skills").fetchone()[0] == "Python"
    assert not (target.app.state.settings.data_dir / "packages/v2.pfcpkg").exists()
    assert apply(target, csrf, token).status_code == 200
    rows = history(target)
    assert [(row["version"], row["status"], row["is_current"]) for row in rows] == [
        (2, "applied", True),
        (1, "applied", False),
    ]
    assert all(row["applied_by"] == "Owner" and row["applied_at"] for row in rows)
    with monkeypatch.context() as patch:
        patch.setattr(packages, "SettingsImporter", importer)
        problem(
            target.post(f"{PREFIX}/settings-packages/rollback", headers={"X-CSRF-Token": csrf}),
            500,
            "PACKAGE_OPERATION_FAILED",
        )
    assert history(target) == rows
    response = target.post(f"{PREFIX}/settings-packages/rollback", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json()["version"] == 1
    assert [(row["version"], row["status"], row["is_current"]) for row in history(target)] == [
        (2, "rolled_back", False),
        (1, "applied", True),
    ]
    with database(target.app.state.settings) as db:
        assert db.execute("SELECT name FROM skills").fetchone()[0] == "Python"


def test_version_high_water_survives_rollback(apps: tuple[TestClient, TestClient]) -> None:
    source, target = apps
    source_csrf, csrf = login(source), login(target)
    versions = []
    for _ in range(2):
        package = base64.b64decode(export(source, source_csrf).json()["content_base64"])
        versions.append(package)
        provision(source, target)
        token = preview(target, csrf, package).json()["token"]
        assert apply(target, csrf, token).status_code == 200
    assert (
        target.post(
            f"{PREFIX}/settings-packages/rollback", headers={"X-CSRF-Token": csrf}
        ).status_code
        == 200
    )
    problem(preview(target, csrf, versions[1]), 409, "PACKAGE_STALE_VERSION")
    third = base64.b64decode(export(source, source_csrf).json()["content_base64"])
    response = preview(target, csrf, third)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 3
    assert apply(target, csrf, response.json()["token"]).status_code == 200
    assert [(row["version"], row["status"], row["is_current"]) for row in history(target)] == [
        (3, "applied", True),
        (2, "rolled_back", False),
        (1, "applied", False),
    ]
    with database(target.app.state.settings) as db:
        rollback = db.execute(
            "SELECT entity_id, details_json FROM audit_log WHERE action='settings_package.rollback'"
        ).fetchone()
        assert rollback["entity_id"] == "2"
        assert '"restored_version":1' in rollback["details_json"]


def test_token_ownership_expiry_and_tampering(
    apps: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = apps
    source_csrf, csrf = login(source), login(target)
    package = base64.b64decode(export(source, source_csrf).json()["content_base64"])
    provision(source, target)
    token = preview(target, csrf, package).json()["token"]
    csrf = login(target)
    problem(apply(target, csrf, token), 403, "PACKAGE_PREVIEW_FORBIDDEN")
    token = preview(target, csrf, package).json()["token"]
    path = target.app.state.settings.data_dir / "packages/staged" / f"{token}.upload"
    path.write_bytes(b"changed")
    problem(apply(target, csrf, token), 409, "PACKAGE_PREVIEW_CHANGED")
    monkeypatch.setattr(staging, "PREVIEW_LIFETIME_SECONDS", 1)
    token = preview(target, csrf, package).json()["token"]
    now = datetime.now(UTC) + timedelta(seconds=3)
    target.app.state.clock = lambda: now
    problem(apply(target, csrf, token), 410, "PACKAGE_PREVIEW_EXPIRED")
    problem(
        target.post(
            f"{PREFIX}/settings-packages/apply",
            headers={"X-CSRF-Token": csrf},
            json={"token": "../../private"},
        ),
        422,
        "VALIDATION_ERROR",
    )


def test_models_and_pending_inbox_apply(apps: tuple[TestClient, TestClient]) -> None:
    source, target = apps
    source_csrf, csrf = login(source), login(target)
    package = base64.b64decode(export(source, source_csrf).json()["content_base64"])
    provision(source, target)
    model = model_package(source)
    response = preview(target, csrf, model, model=True)
    assert response.status_code == 200, response.text
    wrong = apply(target, csrf, response.json()["token"])
    problem(wrong, 422, "PACKAGE_WRONG_KIND")
    confirmed = apply(target, csrf, response.json()["token"], model=True)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["job_id"] > 0
    with database(target.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM embedding_models").fetchone()[0] == 1
    inbox = target.app.state.settings.data_dir / "inbox"
    inbox.mkdir()
    (inbox / "incoming.pfcpkg").write_bytes(package)
    (inbox / "model.pfcmodel").write_bytes(model)
    (inbox / "broken.pfcpkg").write_bytes(b"invalid")
    pending = target.get(f"{PREFIX}/settings-packages/pending").json()
    item = next(item for item in pending if item["file_name"] == "incoming.pfcpkg")
    assert item["valid"] and "path" not in item
    assert history(target) == []
    result = target.post(
        f"{PREFIX}/settings-packages/apply",
        headers={"X-CSRF-Token": csrf},
        json={"pending_id": item["id"]},
    )
    assert result.status_code == 200, result.text
    assert history(target)[0]["version"] == 1
    problem(
        target.post(
            f"{PREFIX}/settings-packages/apply",
            headers={"X-CSRF-Token": csrf},
            json={"pending_id": "f" * 64},
        ),
        404,
        "PACKAGE_NOT_PENDING",
    )


def test_pending_model_rbac(apps: tuple[TestClient, TestClient]) -> None:
    source, target = apps
    source_csrf = login(source)
    assert export(source, source_csrf).status_code == 200
    provision(source, target)
    inbox = target.app.state.settings.data_dir / "inbox"
    inbox.mkdir()
    (inbox / "model.pfcmodel").write_bytes(model_package(source))
    csrf = login(target, role="Analyst", email="installer@example.com")
    item = target.get(f"{PREFIX}/settings-packages/pending").json()[0]
    assert item["valid"] is True
    body = {"pending_id": item["id"]}
    problem(
        target.post(f"{PREFIX}/settings-packages/apply", json=body, headers={"X-CSRF-Token": csrf}),
        422,
        "PACKAGE_WRONG_KIND",
    )
    problem(
        target.post(f"{PREFIX}/models/addons/apply", json=body, headers={"X-CSRF-Token": csrf}),
        403,
        "FORBIDDEN",
    )
    csrf = login(target)
    response = target.post(
        f"{PREFIX}/models/addons/apply", json=body, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text


def test_admin_reembedding_retry(
    apps: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = apps
    source_csrf, csrf = login(source), login(target)
    assert export(source, source_csrf).status_code == 200
    provision(source, target)
    package = model_package(source)
    token = preview(target, csrf, package, model=True).json()["token"]

    def fail(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("queue unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(target.app.state.job_runner, "submit", fail)
        problem(apply(target, csrf, token, model=True), 503, "PACKAGE_REEMBEDDING_PENDING")
    response = preview(target, csrf, package, model=True)
    problem(response, 409, "PACKAGE_MODEL_INSTALLED_PENDING")
    assert "reembed" in response.json()["detail"]
    endpoint = f"{PREFIX}/models/addons/1/reembed"
    problem(target.post(endpoint), 403, "CSRF_INVALID")
    csrf = login(target, role="Analyst", email="installer@example.com")
    problem(target.post(endpoint, headers={"X-CSRF-Token": csrf}), 403, "FORBIDDEN")
    target.cookies.clear()
    problem(target.post(endpoint), 401, "AUTH_REQUIRED")
    csrf = login(target)
    response = target.post(endpoint, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1 and response.json()["job_id"] > 0
    assert target.post(endpoint, headers={"X-CSRF-Token": csrf}).json() == response.json()
    problem(
        target.post(f"{PREFIX}/models/addons/99/reembed", headers={"X-CSRF-Token": csrf}),
        404,
        "PACKAGE_NOT_FOUND",
    )
    with database(target.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM embedding_models").fetchone()[0] == 1


def test_body_limits_and_invalid_multipart(
    apps: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target = apps
    csrf = login(target)
    headers = {"X-CSRF-Token": csrf}
    limit = staging.upload_limit("settings")
    response = target.post(
        f"{PREFIX}/settings-packages/preview",
        content=b"tiny",
        headers={
            **headers,
            "Content-Length": str(limit + 1),
            "Content-Type": "multipart/form-data; boundary=b",
        },
    )
    problem(response, 413, "PACKAGE_TOO_LARGE")
    monkeypatch.setattr(packages, "upload_limit", lambda _: 512)

    def chunks() -> Iterator[bytes]:
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\n'
        yield b"x" * 600
        yield b"\r\n--b--\r\n"

    response = target.post(
        f"{PREFIX}/settings-packages/preview",
        content=chunks(),
        headers={**headers, "Content-Type": "multipart/form-data; boundary=b"},
    )
    problem(response, 413, "PACKAGE_TOO_LARGE")
    problem(
        target.post(f"{PREFIX}/settings-packages/preview", json={}, headers=headers),
        415,
        "PACKAGE_UPLOAD_MEDIA_TYPE",
    )
    problem(
        target.post(
            f"{PREFIX}/settings-packages/preview", files={"wrong": ("a", b"x")}, headers=headers
        ),
        400,
        "PACKAGE_INVALID_UPLOAD",
    )
    assert not list((target.app.state.settings.data_dir / "packages/staged").glob("*.upload"))
