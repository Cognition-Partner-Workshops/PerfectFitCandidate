"""NFR-01: CI-safe ingestion margin, not a full matching benchmark."""

import os
import time

import pytest
from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import data

from pfc.db.connection import database


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("PFC_SKIP_PERF") == "1", reason="PFC_SKIP_PERF=1")
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
