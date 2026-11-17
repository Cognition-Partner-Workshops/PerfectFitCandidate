import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.db.users import create_user
from pfc.jobs.router import router
from pfc.jobs.runner import JobRunner
from pfc.security.problems import install_handlers
from pfc.security.sessions import stamp

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path)
    migrate(settings)
    runner = JobRunner(settings)
    with database(settings) as db:
        for number in (1, 2):
            create_user(
                db,
                email=f"user{number}@example.com",
                display_name="User",
                role_id=1,
                password_hash="unused",
                now=stamp(NOW),
            )
    app = FastAPI()
    app.state.job_runner = runner
    app.state.clock = lambda: NOW
    install_handlers(app)

    @app.middleware("http")
    async def identity(request: Request, call_next):
        if role := request.headers.get("test-role"):
            request.state.principal = SimpleNamespace(
                user=SimpleNamespace(user_id=1), session=SimpleNamespace(role_at_login=role)
            )
        return await call_next(request)

    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as result:
        result.headers["test-role"] = "Analyst"
        yield result
    runner.shutdown()


def insert(
    client: TestClient,
    *,
    owner: int | None = 1,
    status: str = "queued",
    age: int = 0,
    kind: str = "cv_sync",
    params: object = None,
    message: str | None = None,
) -> int:
    with database(client.app.state.job_runner.settings) as db:
        cursor = db.execute(
            "INSERT INTO jobs(kind,params_json,status,message,created_by,created_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                kind,
                json.dumps(params),
                status,
                message,
                owner,
                stamp(NOW - timedelta(minutes=age)),
                stamp(NOW - timedelta(minutes=age))
                if status not in {"queued", "running"}
                else None,
            ),
        )
        return int(cursor.lastrowid or 0)


@pytest.mark.parametrize("role", ["Analyst", "Admin"])
def test_scope_window_order(client: TestClient, role: str) -> None:
    client.headers["test-role"] = role
    old_running = insert(client, status="running", age=100)
    insert(client, status="completed", age=16)
    insert(client, owner=2)
    system = insert(client, owner=None, status="completed", age=1)
    own = insert(client)
    boundary = insert(client, status="failed", age=15)
    assert [item["jobId"] for item in client.get("/api/v1/jobs").json()] == [
        own,
        system,
        boundary,
        old_running,
    ]


@pytest.mark.parametrize(
    ("kind", "params", "label", "link"),
    [
        ("cv_sync", {"resource_ids": [1]}, "CV Sync for 1 profile", "/#/bench"),
        ("cv_sync", {"resource_ids": [1, 2, 3]}, "CV Sync for 3 profiles", "/#/bench"),
        ("cv_sync", None, "CV Sync for all profiles", "/#/bench"),
        ("cv_sync", {"resource_ids": []}, "CV Sync for all profiles", "/#/bench"),
        ("match_run", {"run_id": 12}, "Matching run #12", "/#/matches?runId=12"),
        ("embed", {}, "Re-indexing CVs", None),
        ("embed", {"model_type": "llm"}, "Model check", None),
        ("other_task", [], "Other task", None),
    ],
)
def test_presentation(
    client: TestClient, kind: str, params: object, label: str, link: str | None
) -> None:
    insert(client, kind=kind, params=params)
    item = client.get("/api/v1/jobs").json()[0]
    assert (item["label"], item["link"]) == (label, link)
    assert "params_json" not in item and "created_by" not in item


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "cancelled"])
def test_safe_message(client: TestClient, status: str) -> None:
    insert(client, status=status, message="RuntimeError: private@example.com /home/user/cv.docx")
    item = client.get("/api/v1/jobs").json()[0]
    assert item["message"] == f"Job {status}"


def test_empty_auth_and_limit(client: TestClient) -> None:
    assert client.get("/api/v1/jobs").json() == []
    for _ in range(105):
        insert(client)
    assert len(client.get("/api/v1/jobs").json()) == 20
    assert len(client.get("/api/v1/jobs?limit=100").json()) == 100
    assert len(client.get("/api/v1/jobs?limit=1").json()) == 1
    for value in ("0", "-1", "101", "bad"):
        assert client.get(f"/api/v1/jobs?limit={value}").status_code == 422
    client.headers.pop("test-role")
    assert client.get("/api/v1/jobs").status_code == 401
