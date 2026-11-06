"""NFR-01: CI-safe ingestion margin, not a full matching benchmark."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.ingestion.workbooks import CT, DL
from scripts.generate_uat_data import workbook_bytes


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    password = "Strong!Random4567"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": "owner@example.com",
                "display_name": "Owner",
                "password": password,
            },
        )
        assert response.status_code == 201, response.text
        client.cookies.clear()
        response = client.post(
            "/api/v1/auth/login",
            json={"role": "Admin", "email": "owner@example.com", "password": password},
        )
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


def data(kind: str, rows: list[dict[str, object]]) -> bytes:
    if kind == "dl":
        defaults: dict[str, object] = {
            "Code Requisition": "T11234",
            "Code Local Grade": "B1",
            "JD": "? SQL",
        }
    else:
        defaults = {
            "ShortName": "Alex",
            "Email Address": "alex@example.com",
            "Grade": "B1",
            "Bench Status": "Available",
            "Skills": "SQL",
            "Group": "RG1",
        }
    return workbook_bytes(
        list(DL if kind == "dl" else CT), [defaults | changes for changes in rows]
    )


@pytest.mark.perf
@pytest.mark.parametrize("kind,table", [("dl", "demands"), ("ct", "resources")])
def test_500_row_upload_preview_and_commit_under_60_seconds(
    client: TestClient, capsys: pytest.CaptureFixture[str], kind: str, table: str
) -> None:
    rows: list[dict[str, object]] = [
        {"Code Requisition": f"T{10000 + number}"}
        if kind == "dl"
        else {
            "ShortName": f"Person{number:03d}",
            "Email Address": f"person{number:03d}@example.com",
        }
        for number in range(500)
    ]
    payload = data(kind, rows)
    started = time.perf_counter()
    response = client.post(f"/api/v1/uploads/{kind}", files={"file": (f"{kind}.xlsx", payload)})
    preview_seconds = time.perf_counter() - started
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["counts"]["rows_read"] == 500
    assert run["counts"]["rows_new"] == 500
    assert run["counts"]["rows_rejected"] == 0
    started = time.perf_counter()
    committed = client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit")
    commit_seconds = time.perf_counter() - started
    with capsys.disabled():
        print(
            f"\nPERF {kind.upper()} 500 rows: upload+preview={preview_seconds:.3f}s "
            f"commit={commit_seconds:.3f}s",
            flush=True,
        )
    assert committed.status_code == 200, committed.text
    assert committed.json()["status"] == "committed"
    with database(client.app.state.settings) as db:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 500
    assert preview_seconds < 60
    assert commit_seconds < 60
