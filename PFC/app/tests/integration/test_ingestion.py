"""REQ-ING-01..03/06: real sessions, SQLite transactions and synthetic workbooks."""

import json
import os
import threading
import time
from collections.abc import Iterable, Iterator
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pypdf import PdfWriter

from pfc.adapters.vector.embeddings import FakeEmbedder
from pfc.adapters.vector.stores import create_vector_store
from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.jobs.runner import JobCancelled, JobContext
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.embeddings.chunking import OwnerType
from pfc.services.embeddings.reembed import reembed
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable
from pfc.services.ingestion import cv_sync, service
from pfc.services.ingestion.repository import parse
from pfc.services.ingestion.workbooks import CT, DL


def configure_resume_root(client: TestClient, root: Path, scope: str = "system") -> None:
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO settings(scope,user_id,key,value_json,updated_at) "
            "VALUES (?,?,'resume.root_path',?,'2026-01-01')",
            (scope, 1 if scope == "user" else None, json.dumps(str(root))),
        )


def resume_docx(root: Path, name: str, text: str, mtime: int = 1767225600) -> Path:
    doc = Document()
    doc.add_paragraph(text)
    path = root / name
    doc.save(str(path))
    os.utime(path, (mtime, mtime))
    return path


def wait_cv_job(client: TestClient, response) -> dict:
    assert response.status_code == 202, response.text
    runner = client.app.state.job_runner
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = runner.get(response.json()["job_id"])
        assert job is not None
        if job.status in {"completed", "failed", "cancelled"}:
            assert job.status == "completed", job
            assert job.progress == 1
            return json.loads(job.message)
        time.sleep(0.01)
    pytest.fail("CV job did not finish")


PASSWORD = "Strong!Random4567"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": "owner@example.com",
                "display_name": "Owner",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 201, response.text
        login(client)
        yield client


def login(client: TestClient, role: str = "Admin") -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"role": role, "email": "owner@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def data(kind: str, rows: list[dict[str, object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append(["Synthetic title"])
    headers = list(DL if kind == "dl" else CT)
    sheet.append(headers)
    for changes in rows:
        fields: dict[str, object] = dict.fromkeys(headers, None)
        if kind == "dl":
            fields.update({"Code Requisition": "T11234", "Code Local Grade": "B1", "JD": "? SQL"})
        else:
            fields.update(
                {
                    "ShortName": "Alex",
                    "Email Address": "alex@example.com",
                    "Grade": "B1",
                    "Bench Status": "Available",
                    "Skills": "SQL",
                    "Group": "RG1",
                }
            )
        fields.update(changes)
        sheet.append(list(fields.values()))
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def upload(client: TestClient, kind: str, rows: list[dict[str, object]]) -> dict:
    response = client.post(
        f"/api/v1/uploads/{kind}", files={"file": (f"{kind}.xlsx", data(kind, rows))}
    )
    assert response.status_code == 200, response.text
    return response.json()


def commit(client: TestClient, run: dict) -> dict:
    response = client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit")
    assert response.status_code == 200, response.text
    return response.json()


def test_dl_embedding_trigger_only_changed_versions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[set[tuple[OwnerType, int]]] = []

    def submit(
        runner: object,
        settings: Settings,
        owners: Iterable[tuple[OwnerType, int]],
        *,
        created_by: int | None = None,
    ) -> int:
        assert runner is client.app.state.job_runner and created_by == 1
        calls.append(set(owners))
        return 999

    monkeypatch.setattr("pfc.api.routers.ingestion.submit_incremental_embed", submit)
    commit(client, upload(client, "dl", [{}, {"Code Requisition": "T21234"}]))
    with database(client.app.state.settings) as db:
        old = dict(db.execute("SELECT code_requisition,current_version_id FROM demands"))
    assert calls == [{("jd", id) for id in old.values()}]
    calls.clear()
    rows: list[dict[str, object]] = [
        {"JD": "Python"},
        {"Code Requisition": "T21234"},
        {"Code Requisition": "T31234", "JD": "Rust"},
    ]
    commit(client, upload(client, "dl", rows))
    with database(client.app.state.settings) as db:
        new = dict(db.execute("SELECT code_requisition,current_version_id FROM demands"))
    assert calls == [{("jd", old["T11234"]), ("jd", new["T11234"]), ("jd", new["T31234"])}]
    calls.clear()
    rows[0]["Account Name"] = "Metadata only"
    commit(client, upload(client, "dl", rows))
    commit(client, upload(client, "ct", [{}]))
    assert calls == []


@pytest.mark.parametrize("failure", ["raise", "inactive", "snapshot"])
def test_dl_commit_survives_embedding_trigger_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    def submit(*args: object, **kwargs: object) -> None:
        if failure != "inactive":
            raise RuntimeError("Submission unavailable")

    monkeypatch.setattr("pfc.api.routers.ingestion.submit_incremental_embed", submit)
    if failure == "snapshot":
        monkeypatch.setattr("pfc.api.routers.ingestion._current_versions", submit)
    result = commit(client, upload(client, "dl", [{}]))
    assert result["status"] == "committed"
    assert any(
        record.levelname == "WARNING" and "embedding" in record.message for record in caplog.records
    )
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT current_version_id FROM demands").fetchone()[0] is not None


def test_req_ing_01_03_dl_preview_commit_versions_and_lifecycle(client: TestClient) -> None:
    first = upload(client, "dl", [{}, {"Code Requisition": "T21234"}])
    assert first["status"] == "preview"
    assert first["counts"]["rows_new"] == 2
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM demands").fetchone()[0] == 0
    commit(client, first)
    with database(client.app.state.settings) as db:
        db.execute("UPDATE demands SET occ_reviewed_by=1, occ_reviewed_at='reviewed'")
    second = upload(client, "dl", [{"Account Name": "Changed"}, {"Code Requisition": "T15678"}])
    assert second["diff"]["changed"] == ["T11234"]
    assert second["diff"]["new"] == ["T15678"]
    assert second["diff"]["missing"] == ["T21234"]
    commit(client, second)
    with database(client.app.state.settings) as db:
        demand = db.execute("SELECT * FROM demands WHERE code_requisition='T11234'").fetchone()
        assert demand["row_hash"]
        assert demand["occ_reviewed_by"] is demand["occ_reviewed_at"] is None
        assert (
            db.execute("SELECT lifecycle FROM demands WHERE code_requisition='T21234'").fetchone()[
                0
            ]
            == "closed"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM demand_versions WHERE demand_id=?", (demand["demand_id"],)
            ).fetchone()[0]
            == 1
        )
        version = db.execute(
            "SELECT * FROM demand_versions WHERE demand_id=?", (demand["demand_id"],)
        ).fetchone()
        assert version["jd_text_raw"] == "? SQL"
        assert version["jd_text_norm"] == "- SQL"
    third = upload(
        client, "dl", [{"Account Name": "Changed", "JD": "Python"}, {"Code Requisition": "T15678"}]
    )
    assert third["counts"]["rows_unchanged"] == 1
    commit(client, third)
    with database(client.app.state.settings) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM demand_versions WHERE demand_id=?", (demand["demand_id"],)
            ).fetchone()[0]
            == 2
        )
        assert verify_chain(db)


def test_req_ing_03_ct_commit_verbatim_candidates_and_off_bench(client: TestClient) -> None:
    first = upload(
        client,
        "ct",
        [
            {"Email Address": " AlEx@Example.com "},
            {"ShortName": "Taylor", "Email Address": "taylor@example.com"},
        ],
    )
    commit(client, first)
    with database(client.app.state.settings) as db:
        row = db.execute("SELECT * FROM resources WHERE short_name_key='alex'").fetchone()
        assert row["email_address"] == " AlEx@Example.com "
        assert row["email_key"] == "alex@example.com"
        assert row["eligibility"] == "eligible"
        assert row["bench_status_code"] == "AVAILABLE"
        assert (
            db.execute(
                "SELECT email FROM candidate_emails WHERE candidate_id=?", (row["candidate_id"],)
            ).fetchone()[0]
            == " AlEx@Example.com "
        )
        db.execute("UPDATE resources SET occ_reviewed_by=1, occ_reviewed_at='reviewed'")
    changed = upload(
        client,
        "ct",
        [
            {"Email Address": " AlEx@Example.com ", "Skills": "Python"},
            {"ShortName": "Morgan", "Email Address": "morgan@example.com"},
        ],
    )
    assert changed["counts"]["rows_new"] == changed["counts"]["rows_updated"] == 1
    assert changed["diff"]["missing"] == [["taylor", "taylor@example.com"]]
    commit(client, changed)
    with database(client.app.state.settings) as db:
        assert (
            db.execute("SELECT lifecycle FROM resources WHERE short_name_key='taylor'").fetchone()[
                0
            ]
            == "off_bench"
        )
        row = db.execute("SELECT * FROM resources WHERE short_name_key='alex'").fetchone()
        assert row["occ_reviewed_by"] is row["occ_reviewed_at"] is None
        assert db.execute("SELECT count(*) FROM candidates").fetchone()[0] == 3
        assert verify_chain(db)


@pytest.mark.parametrize(
    "changes,expected,reason",
    [
        ({"Bench Status": "Selected"}, "excluded", "STATUS_EXCLUDED"),
        ({"Group": "Not on Bench"}, "excluded", "NOT_ON_BENCH"),
        ({"Bench End Date": "nonsense"}, "eligible", None),
        ({"Bench Status": "Custom"}, "eligible", None),
    ],
)
def test_req_elig_01_stored_domain_eligibility(
    client: TestClient, changes: dict, expected: str, reason: str | None
) -> None:
    run = upload(client, "ct", [changes])
    commit(client, run)
    with database(client.app.state.settings) as db:
        row = db.execute("SELECT * FROM resources").fetchone()
        assert row["eligibility"] == expected
        assert row["exclusion_reason"] == reason
        if changes.get("Bench End Date"):
            assert row["bench_end_date"] is None
            assert row["bench_end_date_parse_status"] == "unparseable"
            assert any(i["code"] == "DATE_UNPARSEABLE" for i in run["issues"])


def test_req_ing_06_noop_and_commit_twice(client: TestClient) -> None:
    payload = data("dl", [{}])
    first = client.post("/api/v1/uploads/dl", files={"file": ("dl.xlsx", payload)}).json()
    commit(client, first)
    assert (
        client.post(f"/api/v1/ingestion-runs/{first['ingestion_run_id']}/commit").status_code == 409
    )
    same = client.post("/api/v1/uploads/dl", files={"file": ("dl.xlsx", payload)}).json()
    assert same["status"] == "no_op" and same["no_op"]
    assert (
        client.post(f"/api/v1/ingestion-runs/{same['ingestion_run_id']}/commit").status_code == 409
    )
    with database(client.app.state.settings) as db:
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log")]
        assert all(a in actions for a in ["UPLOAD_PREVIEW", "INGEST_COMMIT", "INGEST_NO_OP"])
        assert all(
            "example.com" not in (r[0] or "")
            for r in db.execute("SELECT details_json FROM audit_log")
        )
        assert verify_chain(db)


def test_req_ing_02_discard_history_and_issues(client: TestClient) -> None:
    run = upload(client, "dl", [{"Position Start Date": "bad"}])
    run_id = run["ingestion_run_id"]
    assert client.get(f"/api/v1/ingestion-runs/{run_id}").json() == run
    page = client.get(f"/api/v1/ingestion-runs/{run_id}/issues?limit=1").json()
    assert page["total"] == 1 and page["items"][0]["raw_value"] == "bad"
    assert client.get("/api/v1/ingestion-runs?limit=1").json()["total"] == 1
    response = client.post(f"/api/v1/ingestion-runs/{run_id}/discard")
    assert response.json()["status"] == "discarded"
    assert not (client.app.state.settings.data_dir / "uploads" / f"{run_id}.xlsx").exists()
    assert client.post(f"/api/v1/ingestion-runs/{run_id}/commit").status_code == 409
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT 1 FROM audit_log WHERE action='INGEST_DISCARD'").fetchone()
        assert verify_chain(db)


def test_req_ing_01_auth_csrf_and_analyst(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/ingestion-runs").status_code == 401
    assert (
        client.post("/api/v1/uploads/dl", files={"file": ("dl.xlsx", data("dl", [{}]))}).status_code
        == 401
    )
    login(client, "Analyst")
    csrf = client.headers.pop("X-CSRF-Token")
    assert (
        client.post("/api/v1/uploads/dl", files={"file": ("dl.xlsx", data("dl", [{}]))}).status_code
        == 403
    )
    client.headers["X-CSRF-Token"] = csrf
    run = upload(client, "dl", [{}])
    assert commit(client, run)["status"] == "committed"


def test_req_ing_01_invalid_failed_and_oversize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.post("/api/v1/uploads/dl", files={"file": ("bad.xlsx", b"not a workbook")})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert client.get("/api/v1/ingestion-runs").json()["items"][0]["status"] == "failed"
    assert (
        client.post("/api/v1/uploads/dl", files={"file": ("bad.xlsm", b"bad")}).status_code == 422
    )
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 10)
    assert (
        client.post("/api/v1/uploads/dl", files={"file": ("large.xlsx", b"x" * 11)}).status_code
        == 413
    )
    assert client.get("/api/v1/ingestion-runs/9999").status_code == 404


def test_req_ing_03_hash_guard_and_commit_rediff(client: TestClient) -> None:
    first = upload(client, "dl", [{}])
    second = upload(client, "dl", [{}])
    commit(client, first)
    committed = commit(client, second)
    assert committed["counts"]["rows_new"] == 0
    assert committed["counts"]["rows_unchanged"] == 1
    tampered = upload(client, "dl", [{"JD": "changed"}])
    (
        client.app.state.settings.data_dir / "uploads" / f"{tampered['ingestion_run_id']}.xlsx"
    ).write_bytes(b"tampered")
    assert (
        client.post(f"/api/v1/ingestion-runs/{tampered['ingestion_run_id']}/commit").status_code
        == 409
    )


def test_req_in_23_incomplete_skip_ambiguous_and_never_off_bench(client: TestClient) -> None:
    unique = upload(client, "ct", [{"Email Address": None}])
    assert unique["diff"]["incomplete"] == [3]
    commit(client, unique)
    ambiguous = upload(
        client,
        "ct",
        [
            {"ShortName": "Taylor", "Email Address": None},
            {"ShortName": "Taylor", "Email Address": None},
        ],
    )
    assert any(i["code"] == "AMBIGUOUS_IDENTITY" for i in ambiguous["issues"])
    commit(client, ambiguous)
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM resources").fetchone()[0] == 1
        assert db.execute("SELECT lifecycle FROM resources").fetchone()[0] == "active"
        assert db.execute("SELECT identity_status FROM candidates").fetchone()[0] == "incomplete"


def test_req_ing_03_unknown_grades_null_fk_and_row_hash(client: TestClient) -> None:
    for kind, field, table in [("dl", "Code Local Grade", "demands"), ("ct", "Grade", "resources")]:
        run = upload(client, kind, [{field: "Z9"}])
        commit(client, run)
        with database(client.app.state.settings) as db:
            record = db.execute(f"SELECT * FROM {table}").fetchone()
            assert record["code_local_grade" if kind == "dl" else "grade"] is None
            preview = parse(
                db,
                client.app.state.settings.data_dir / "uploads" / f"{run['ingestion_run_id']}.xlsx",
                "DL" if kind == "dl" else "CT",
            )
            assert record["row_hash"] == preview.rows[0].row_hash


def test_req_ing_03_commit_recomputes_current_status_mapping(client: TestClient) -> None:
    first = upload(client, "ct", [{}])
    pending = upload(client, "ct", [{}])
    commit(client, first)
    with database(client.app.state.settings) as db:
        db.execute("UPDATE status_map SET is_excluded=1 WHERE lower(source_value)='available'")
    commit(client, pending)
    with database(client.app.state.settings) as db:
        assert (
            db.execute("SELECT exclusion_reason FROM resources").fetchone()[0] == "STATUS_EXCLUDED"
        )


def test_req_ing_03_commit_rollback_on_ambiguous_email(client: TestClient) -> None:
    run = upload(client, "ct", [{}, {"ShortName": "Taylor", "Email Address": "taylor@example.com"}])
    with database(client.app.state.settings) as db:
        for number in (10, 11):
            db.execute(
                "INSERT INTO candidates(candidate_id, internal_ref, identity_status, "
                "created_at, updated_at) "
                "VALUES (?,?,'ambiguous','2026-01-01','2026-01-01')",
                (number, f"PFC-C-{number:06d}"),
            )
            db.execute(
                "INSERT INTO candidate_emails(candidate_id,email,email_key,source) "
                "VALUES (?,?,?,'CT')",
                (number, "taylor@example.com", "taylor@example.com"),
            )
    response = client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit")
    assert response.status_code == 409
    assert response.json()["code"] == "AMBIGUOUS_IDENTITY"
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM resources").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM candidates").fetchone()[0] == 2
        assert db.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "preview"
        assert not db.execute("SELECT 1 FROM audit_log WHERE action='INGEST_COMMIT'").fetchone()
        assert verify_chain(db)


def test_req_ing_02_empty_preview_and_unseeded_references(client: TestClient) -> None:
    empty = upload(client, "dl", [])
    assert empty["counts"]["rows_read"] == 0
    assert empty["issues"] == []
    with database(client.app.state.settings) as db:
        db.execute("DELETE FROM status_map")
        db.execute("DELETE FROM bench_statuses")
        db.execute("DELETE FROM grades")
    run = upload(client, "ct", [{}])
    assert {i["code"] for i in run["issues"]} == {"UNKNOWN_GRADE", "UNMAPPED_STATUS"}
    commit(client, run)
    with database(client.app.state.settings) as db:
        row = db.execute("SELECT * FROM resources").fetchone()
        assert row["grade"] is row["bench_status_code"] is None
        assert row["exclusion_reason"] == "NO_GRADE"


@pytest.fixture
def demand_rows(client: TestClient) -> TestClient:
    commit(client, upload(client, "dl", [{"Code Requisition": "T19999"}]))
    commit(
        client,
        upload(
            client,
            "dl",
            [
                {
                    "Code Requisition": "T10001",
                    "Position Start Date": "1/1/2026",
                    "BU": "Data",
                    "Region": "North",
                    "Account Name": "Acme",
                    "Demand Request Name": "SQL Lead",
                },
                {
                    "Code Requisition": "T10002",
                    "Position Start Date": "2/1/2026",
                    "BU": "Apps",
                    "Region": "South",
                    "Code Local Grade": "A3",
                },
                {"Code Requisition": "T10003", "Position Start Date": "2/1/2026"},
                {"Code Requisition": "T10004"},
            ],
        ),
    )
    return client


def test_req_out_01_demand_paging_sort_and_detail(demand_rows: TestClient) -> None:
    client = demand_rows
    login(client, "Analyst")
    response = client.get("/api/v1/demands?page_size=2")
    page = response.json()
    assert page["total"] == 4 and page["page"] == 1 and page["page_size"] == 2
    assert [r["code_requisition"] for r in page["items"]] == ["T10002", "T10003"]
    second = client.get("/api/v1/demands?page=2&page_size=2").json()
    assert [r["code_requisition"] for r in second["items"]] == ["T10001", "T10004"]
    assert "jd_text_norm" not in response.text
    assert all(r["occ_pending_review"] for r in page["items"])
    demand_id = page["items"][0]["demand_id"]
    detail = client.get(f"/api/v1/demands/{demand_id}").json()
    assert detail["versions"][0]["version_no"] == 1
    assert detail["versions"][0]["jd_text_norm"] == "- SQL"
    assert len(detail["versions"][0]["jd_sha256"]) == 64
    assert detail["versions"][0]["created_at"]
    with database(client.app.state.settings) as db:
        db.execute("UPDATE demands SET occ_reviewed_at='reviewed' WHERE demand_id=?", (demand_id,))
    assert not client.get(f"/api/v1/demands/{demand_id}").json()["occ_pending_review"]
    assert client.get("/api/v1/demands?page=9").json()["items"] == []


@pytest.mark.parametrize(
    "query,expected",
    [
        ("bu=Data", ["T10001"]),
        ("region=North", ["T10001"]),
        ("account_name=Acme", ["T10001"]),
        ("grade=A3", ["T10002"]),
        ("lifecycle=closed", ["T19999"]),
        ("q=sql", ["T10001"]),
        ("q=0004", ["T10004"]),
        ("q=%25", []),
        ("bu=Data&region=South", []),
        ("q=' OR 1=1 --", []),
    ],
)
def test_req_out_01_demand_filters(
    demand_rows: TestClient, query: str, expected: list[str]
) -> None:
    page = demand_rows.get(f"/api/v1/demands?{query}").json()
    assert [r["code_requisition"] for r in page["items"]] == expected
    assert page["total"] == len(expected)


@pytest.fixture
def resource_rows(client: TestClient) -> TestClient:
    commit(
        client,
        upload(client, "ct", [{"ShortName": "Removed", "Email Address": "removed@example.com"}]),
    )
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {
                    "ShortName": "Alex",
                    "Email Address": " AlEx@Example.com ",
                    "Bench Start Date": "1/1/2026",
                    "Bench End Date": "nonsense",
                    "Location": "Edinburgh",
                    "Available Capacity": "N",
                },
                {
                    "ShortName": "Taylor",
                    "Email Address": "taylor@example.com",
                    "Grade": "A3",
                    "Bench Start Date": "2/1/2026",
                    "Bench Status": "Selected",
                    "Location": "London",
                },
                {
                    "ShortName": "Morgan",
                    "Email Address": "morgan@example.com",
                    "Bench Start Date": "2/1/2026",
                    "Bench Status": "Custom",
                    "Group": "RG2",
                },
                {"ShortName": "Casey", "Email Address": None},
            ],
        ),
    )
    return client


def test_req_out_03_resource_sort_mask_flags_and_detail(resource_rows: TestClient) -> None:
    client = resource_rows
    login(client, "Analyst")
    response = client.get("/api/v1/resources")
    rows = response.json()["items"]
    assert [r["short_name"] for r in rows] == ["Taylor", "Morgan", "Alex", "Casey"]
    assert response.json()["total"] == 4
    assert all(
        email not in response.text.lower()
        for email in [
            "alex@example.com",
            "taylor@example.com",
            "morgan@example.com",
            "removed@example.com",
        ]
    )
    assert "email_key" not in response.text
    alex = rows[2]
    assert alex["email_address"] == "A***@Example.com"
    assert alex["pending_review"] and alex["occ_pending_review"]
    assert alex["available_capacity"] == "N" and alex["eligibility"] == "eligible"
    assert rows[1]["unmapped_status"] and not rows[1]["pending_review"]
    assert rows[0]["exclusion_reasons"] == ["STATUS_EXCLUDED"]
    assert rows[3]["email_address"] is None
    assert client.get("/api/v1/resources?page_size=1&page=3").json()["items"] == [alex]
    assert client.get("/api/v1/resources?page=9").json()["items"] == []
    detail_response = client.get(f"/api/v1/resources/{alex['resource_id']}")
    detail = detail_response.json()
    assert "alex@example.com" not in detail_response.text.lower()
    assert detail["candidate_id"] and detail["cv_versions_count"] == 0
    assert detail["latest_cv"] is None
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE resources SET occ_reviewed_at='reviewed' WHERE resource_id=?",
            (alex["resource_id"],),
        )
        resume = db.execute(
            "INSERT INTO resumes(candidate_id,source_path,file_name,file_sha256,"
            "mime_type,discovered_at) "
            "VALUES (?,'alex@example.com.pdf','alex@example.com.pdf','file-hash',"
            "'application/pdf','2026-01-01')",
            (detail["candidate_id"],),
        ).lastrowid
        for version in (1, 2):
            db.execute(
                "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
                "extracted_by,is_latest,extracted_at) VALUES (?,?,?,?,?,'test',?,'2026-01-01')",
                (
                    resume,
                    detail["candidate_id"],
                    version,
                    f"hash-{version}",
                    "alex@example.com",
                    version == 2,
                ),
            )
    detail_response = client.get(f"/api/v1/resources/{alex['resource_id']}")
    detail = detail_response.json()
    assert detail["cv_versions_count"] == 2 and detail["latest_cv"]["version_no"] == 2
    assert not detail["occ_pending_review"]
    assert "alex@example.com" not in detail_response.text.lower()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("grade=A3", ["Taylor"]),
        ("bench_status_code=AVAILABLE", ["Alex", "Casey"]),
        ("eligibility=excluded", ["Taylor"]),
        ("lifecycle=off_bench", ["Removed"]),
        ("group_name=RG2", ["Morgan"]),
        ("location=DIN", ["Alex"]),
        ("q=ayL", ["Taylor"]),
        ("q=%25", []),
        ("grade=A3&location=Edinburgh", []),
    ],
)
def test_req_out_03_resource_filters(
    resource_rows: TestClient, query: str, expected: list[str]
) -> None:
    page = resource_rows.get(f"/api/v1/resources?{query}").json()
    assert [r["short_name"] for r in page["items"]] == expected
    assert page["total"] == len(expected)


def test_req_elig_08_excluded_route_and_reasons(resource_rows: TestClient) -> None:
    client = resource_rows
    response = client.get("/api/v1/resources/excluded?eligibility=eligible")
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["short_name"] == "Taylor"
    assert page["items"][0]["exclusion_reasons"] == ["STATUS_EXCLUDED"]
    assert "taylor@example.com" not in response.text
    assert client.get("/api/v1/resources/excluded?grade=B1").json()["items"] == []


@pytest.mark.parametrize("path", ["/demands", "/resources", "/resources/excluded"])
def test_req_out_read_errors_and_empty(client: TestClient, path: str) -> None:
    assert client.get(f"/api/v1{path}").json()["total"] == 0
    for params in ("page=0", "page_size=0", "page_size=201"):
        assert client.get(f"/api/v1{path}?{params}").status_code == 422
    assert client.get(f"/api/v1{path}?page_size=200").status_code == 200
    if path != "/resources/excluded":
        response = client.get(f"/api/v1{path}/99999")
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
    client.cookies.clear()
    assert client.get(f"/api/v1{path}").status_code == 401
    if path != "/resources/excluded":
        assert client.get(f"/api/v1{path}/1").status_code == 401


@pytest.mark.parametrize(
    "email,masked",
    [
        ("invalid", "***"),
        ("@example.com", "***"),
        ("alex@", "***"),
        ("alex@@example.com", "***"),
        ("", None),
    ],
)
def test_req_out_03_invalid_emails_not_disclosed(
    client: TestClient, email: str, masked: str | None
) -> None:
    commit(client, upload(client, "ct", [{"Email Address": email}]))
    response = client.get("/api/v1/resources")
    row = response.json()["items"][0]
    assert row["email_address"] == masked
    detail = client.get(f"/api/v1/resources/{row['resource_id']}")
    assert detail.json()["email_address"] == masked
    if email:
        assert email not in response.text and email not in detail.text


def test_req_in_21_24_cv_sync_versions_and_metadata(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {},
                {"ShortName": "Taylor", "Email Address": "taylor@example.com"},
                {"ShortName": "Missing", "Email Address": "missing@example.com"},
            ],
        ),
    )
    resume_docx(root, "CV.docx", "SQL Engineer AlEx@Example.com")
    resume_docx(root, "Taylor.docx", "Python Engineer", 1767312000)
    login(client, "Analyst")
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary == {
        "scanned": 2,
        "new_versions": 2,
        "unchanged": 0,
        "missing_cv": 1,
        "ambiguous": 0,
        "unmatched": 0,
        "extraction_failed": 0,
        "embedded": 0,
        "warnings": 0,
        "embedding_counts": {"embedded": 0, "reused": 0, "skipped": 0, "deleted": 0},
        "embedding_notes": ["Embedding skipped: no active embedding model"],
    }
    with database(client.app.state.settings) as db:
        alex = db.execute("SELECT * FROM resources WHERE short_name_key='alex'").fetchone()
        version = db.execute(
            "SELECT * FROM cv_versions WHERE candidate_id=?", (alex["candidate_id"],)
        ).fetchone()
        assert version["identity_match"] == "email_exact"
        assert json.loads(version["facts_json"])["emails"] == ["AlEx@Example.com"]
        assert version["extracted_by"] == "rule_based" and version["is_latest"] == 1
        assert (
            db.execute(
                "SELECT identity_match FROM cv_versions WHERE candidate_id<>(?)",
                (alex["candidate_id"],),
            ).fetchone()[0]
            == "name_fuzzy"
        )
        assert verify_chain(db)
    response = client.get(f"/api/v1/candidates/{alex['candidate_id']}/cv-versions")
    assert response.status_code == 200
    assert response.json()[0]["source_path"] == "CV.docx"
    assert response.json()[0]["file_name"] == "CV.docx"
    assert response.json()[0]["file_mtime"]
    assert "SQL Engineer" not in response.text and "text_norm" not in response.text
    repeat = wait_cv_job(client, client.post("/api/v1/resources/cv-sync", json={}))
    assert repeat["unchanged"] == 2 and repeat["new_versions"] == 0
    resume_docx(root, "CV.docx", "SQL and Python Engineer AlEx@Example.com", 1767400000)
    updated = wait_cv_job(client, client.post(f"/api/v1/resources/{alex['resource_id']}/cv-sync"))
    assert updated["new_versions"] == 1
    versions = client.get(f"/api/v1/candidates/{alex['candidate_id']}/cv-versions").json()
    assert [v["version_no"] for v in versions] == [2, 1]
    assert [v["is_latest"] for v in versions] == [True, False]
    with database(client.app.state.settings) as db:
        audit = db.execute(
            "SELECT details_json FROM audit_log WHERE action='CV_SYNC' ORDER BY audit_id DESC"
        ).fetchone()[0]
        assert json.loads(audit) == updated
        assert "example.com" not in audit and str(root) not in audit
        assert verify_chain(db)


def test_req_in_25_cv_sync_ambiguous_unmatched_txt_pdf_failures(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {},
                {"ShortName": "Taylor", "Email Address": "taylor@example.com"},
            ],
        ),
    )
    resume_docx(root, "Shared.docx", "alex@example.com taylor@example.com")
    resume_docx(root, "Unknown.docx", "An unrelated CV")
    (root / "Alex.txt").write_text("alex@example.com SQL")
    (root / "alex@example.com.pdf").write_bytes(b"broken pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(root / "Taylor.pdf")
    (root / "~$Alex.docx").write_bytes(b"lock")
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["scanned"] == 5 and summary["new_versions"] == 4
    assert summary["ambiguous"] == 1 and summary["unmatched"] == 1
    assert summary["extraction_failed"] == 2 and summary["warnings"] == 1
    with database(client.app.state.settings) as db:
        ambiguous = db.execute(
            "SELECT * FROM cv_versions WHERE identity_match='ambiguous'"
        ).fetchone()
        assert ambiguous["candidate_id"] is None and not ambiguous["is_latest"]
        assert (
            db.execute(
                "SELECT candidate_id FROM cv_versions WHERE identity_match='none'"
            ).fetchone()[0]
            is None
        )
        assert (
            db.execute(
                "SELECT count(*) FROM resumes WHERE mime_type NOT IN "
                "('application/pdf',"
                "'application/vnd.openxmlformats-officedocument.wordprocessingml.document')"
            ).fetchone()[0]
            == 0
        )
    repeat = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeat["new_versions"] == 0 and repeat["unchanged"] == 4


def test_req_mat_01_cv_sync_optional_embedding(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "SQL Python " * 220)
    client.app.state.embedder = FakeEmbedder(dimensions=8)
    no_model = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert no_model["embedded"] == 0
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active) "
            "VALUES ('fake','onnx','',8,'fixed-words-v1',1)"
        )
    embedded = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert embedded["embedded"] == 2 and embedded["new_versions"] == 0
    with database(client.app.state.settings) as db:
        chunks = db.execute("SELECT * FROM embedding_chunks").fetchall()
        assert len(chunks) == 2
        assert all(c["owner_type"] == "cv" and c["section"] == "full" for c in chunks)
        store = create_vector_store(db, dimensions=8)
        assert store.count(model_version=client.app.state.embedder.model_version) == 2
        matches = store.search(
            client.app.state.embedder.embed(["SQL Python"])[0],
            model_version=client.app.state.embedder.model_version,
            namespace="cv",
        )
        assert len(matches) == 2
    repeated = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeated["embedded"] == 0


def test_req_in_21_cv_sync_path_auth_and_selection(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/v1/resources/cv-sync")
    assert response.status_code == 409 and response.json()["code"] == "RESUME_PATH_NOT_CONFIGURED"
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    configure_resume_root(client, tmp_path / "missing", scope="user")
    assert client.post("/api/v1/resources/cv-sync").status_code == 409
    with database(client.app.state.settings) as db:
        db.execute("DELETE FROM settings WHERE scope='user'")
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "SQL")
    empty = wait_cv_job(client, client.post("/api/v1/resources/cv-sync", json={"resource_ids": []}))
    assert empty["new_versions"] == 0
    assert client.post("/api/v1/resources/999/cv-sync").status_code == 404
    assert client.get("/api/v1/candidates/999/cv-versions").status_code == 404
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v1/resources/cv-sync").status_code == 403
    assert client.post("/api/v1/resources/1/cv-sync").status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    client.cookies.clear()
    assert client.post("/api/v1/resources/cv-sync").status_code == 401
    assert client.post("/api/v1/resources/1/cv-sync").status_code == 401
    assert client.get("/api/v1/candidates/1/cv-versions").status_code == 401


def test_req_in_24_cv_file_dates_and_candidate_email_aliases(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    with database(client.app.state.settings) as db:
        candidate_id = db.execute("SELECT candidate_id FROM resources").fetchone()[0]
        db.execute(
            "INSERT INTO candidate_emails(candidate_id,email,email_key,source) "
            "VALUES (?,'Second@Example.com','second@example.com','manual')",
            (candidate_id,),
        )
    resume_docx(root, "Alex recent.docx", "Second@Example.com SQL", 1767400000)
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync", json={"resource_ids": [1]}))
    resume_docx(root, "Alex old.docx", "alex@example.com Python", 1767225600)
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["ambiguous"] == 0 and summary["new_versions"] == 1
    history = client.get(f"/api/v1/candidates/{candidate_id}/cv-versions").json()
    assert [(v["file_name"], v["version_no"], v["is_latest"]) for v in history] == [
        ("Alex recent.docx", 2, True),
        ("Alex old.docx", 1, False),
    ]
    assert all(v["identity_match"] == "email_exact" for v in history)


def test_req_in_21_cv_cancellation_before_persistence(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "SQL Engineer")
    context = JobContext(client.app.state.job_runner, 0, threading.Event())
    original = cv_sync.ResumeFolderSource.scan

    def cancel_after_scan(source, candidates):
        result = original(source, candidates)
        context.cancelled.set()
        return result

    monkeypatch.setattr(cv_sync.ResumeFolderSource, "scan", cancel_after_scan)
    with pytest.raises(JobCancelled):
        cv_sync.sync(context, client.app.state.settings, root, [1], 1)
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM resumes").fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM audit_log WHERE action='CV_SYNC'").fetchone()


def test_req_mat_01_cv_embedding_failure_preserves_candidate(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "SQL")
    client.app.state.embedder = FakeEmbedder(dimensions=8)
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active) "
            "VALUES ('fake','onnx','fake-hash',16,'fixed-words-v1',1)"
        )
    response = client.post("/api/v1/resources/cv-sync")
    assert response.status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.app.state.job_runner.get(response.json()["job_id"])
        if job.status in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert job.status == "failed" and job.message == "Job failed"
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM resumes").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM cv_versions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM embedding_chunks").fetchone()[0] == 0


@pytest.mark.parametrize("full_first", [False, True])
def test_cv_sync_and_full_reembed_do_not_churn(
    client: TestClient, tmp_path: Path, full_first: bool
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "SQL Python " * 220)
    settings = client.app.state.settings
    embedder = FakeEmbedder(8)
    client.app.state.embedder = embedder
    offline = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert offline["embedding_notes"]
    with database(settings) as db:
        db.execute(
            "UPDATE cv_versions SET sections_json=?",
            (json.dumps({"skills": "SQL Python", "experience": ["Software Engineer"]}),),
        )
        id = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active) "
            "VALUES ('fake','onnx','',8,'v1',1) RETURNING embedding_model_id"
        ).fetchone()[0]
    if not full_first:
        wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(settings) as db:
        store = create_vector_store(db, dimensions=8)
        ctx = JobContext(client.app.state.job_runner, 0, threading.Event())
        result = reembed(settings, ctx, embedding_model_id=id, embedder=embedder, store=store)
        if not full_first:
            assert result.embedded == result.deleted == 0
        before = [
            tuple(row) for row in db.execute("SELECT * FROM embedding_chunks ORDER BY chunk_id")
        ]
        assert {row["section"] for row in db.execute("SELECT section FROM embedding_chunks")} == {
            "skills",
            "experience/0",
        }
    repeated = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeated["embedding_counts"]["embedded"] == repeated["embedding_counts"]["deleted"] == 0
    with database(settings) as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM embedding_chunks ORDER BY chunk_id")
        ] == before


@pytest.mark.parametrize("available", [True, False])
def test_cv_sync_runtime_fallback_and_latest_cleanup(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: bool
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "SQL")
    settings = client.app.state.settings
    with database(settings) as db:
        model_id = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active) "
            "VALUES ('fake','onnx','',8,'v1',1) RETURNING embedding_model_id"
        ).fetchone()[0]
    calls: list[int] = []

    def factory(settings: Settings, *, embedding_model_id: int) -> FakeEmbedder:
        calls.append(embedding_model_id)
        if not available:
            raise EmbeddingModelUnavailable(
                f"Model files not found at {settings.bundled_models_dir}"
            )
        return FakeEmbedder(8)

    monkeypatch.setattr(cv_sync, "create_embedder", factory)
    first = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert calls == [model_id] and first["new_versions"] == 1
    if not available:
        assert "Model files not found" in first["embedding_notes"][0]
        assert first["embedded"] == 0
    else:
        assert first["embedded"] == 1
        available = False
        resume_docx(root, "Alex.docx", "Python", 1893456000)
        skipped = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
        assert skipped["new_versions"] == 1 and skipped["embedded"] == 0
        assert skipped["embedding_notes"]
        available = True
        updated = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
        assert updated["new_versions"] == 0
        assert updated["embedding_counts"]["deleted"] == 1
        assert updated["embedded"] == 1
        with database(settings) as db:
            owner = db.execute("SELECT owner_id FROM embedding_chunks").fetchone()[0]
            assert (
                db.execute(
                    "SELECT is_latest FROM cv_versions WHERE cv_version_id=?", (owner,)
                ).fetchone()[0]
                == 1
            )


def test_req_in_25_identical_files_for_different_candidates_not_attached(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {},
                {"ShortName": "Taylor", "Email Address": "taylor@example.com"},
            ],
        ),
    )
    first = resume_docx(root, "Alex.docx", "SQL Engineer")
    (root / "Taylor.docx").write_bytes(first.read_bytes())
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["ambiguous"] == 2 and summary["new_versions"] == 1
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT candidate_id FROM resumes").fetchone()[0] is None
        assert db.execute("SELECT candidate_id FROM cv_versions").fetchone()[0] is None


def test_req_in_24_renamed_resume_updates_metadata_without_new_version(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    first = resume_docx(root, "Original.docx", "alex@example.com SQL")
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    first.rename(root / "Renamed.docx")
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == 0 and summary["unchanged"] == 1
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT source_path FROM resumes").fetchone()[0] == "Renamed.docx"


def test_req_in_24_previously_unmatched_cv_attaches_after_identity_resolves(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Old.docx", "morgan@example.com SQL", 1767225600)
    first = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert first["unmatched"] == 1 and first["new_versions"] == 1
    with database(client.app.state.settings) as db:
        orphan_id = db.execute("SELECT cv_version_id FROM cv_versions").fetchone()[0]
        assert db.execute("SELECT candidate_id FROM cv_versions").fetchone()[0] is None
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {},
                {"ShortName": "Morgan", "Email Address": "morgan@example.com"},
            ],
        ),
    )
    resume_docx(root, "Recent.docx", "morgan@example.com Python", 1767400000)
    resolved = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert resolved["new_versions"] == 1 and resolved["unchanged"] == 1
    with database(client.app.state.settings) as db:
        candidate_id = db.execute(
            "SELECT candidate_id FROM resources WHERE short_name_key='morgan'"
        ).fetchone()[0]
        assert (
            db.execute("SELECT candidate_id FROM resumes WHERE source_path='Old.docx'").fetchone()[
                0
            ]
            == candidate_id
        )
    history = client.get(f"/api/v1/candidates/{candidate_id}/cv-versions").json()
    assert [(v["version_no"], v["is_latest"]) for v in history] == [(2, True), (1, False)]
    assert history[1]["cv_version_id"] == orphan_id
    assert all(v["identity_match"] == "email_exact" for v in history)
    repeat = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeat["new_versions"] == 0 and repeat["unchanged"] == 2


def test_req_in_24_identical_copy_keeps_original_cv_provenance(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    original = resume_docx(root, "Alex original.docx", "alex@example.com SQL", 1767225600)
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        before = dict(db.execute("SELECT * FROM resumes").fetchone())
        candidate_id = before["candidate_id"]
    copy = root / "Alex copy.docx"
    copy.write_bytes(original.read_bytes())
    os.utime(copy, (1767400000, 1767400000))
    for _ in range(2):
        summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
        assert summary["new_versions"] == 0
        with database(client.app.state.settings) as db:
            assert dict(db.execute("SELECT * FROM resumes").fetchone()) == before
        history = client.get(f"/api/v1/candidates/{candidate_id}/cv-versions").json()
        assert len(history) == 1
        assert history[0]["source_path"] == "Alex original.docx"
        assert history[0]["file_name"] == "Alex original.docx"
        assert history[0]["file_mtime"] == before["file_mtime"]


def test_req_ing_03_failed_commit_preserves_original_preview_sidecar(client: TestClient) -> None:
    pending = upload(
        client,
        "ct",
        [
            {},
            {"ShortName": "Taylor", "Email Address": "taylor@example.com"},
        ],
    )
    run_id = pending["ingestion_run_id"]
    sidecar = client.app.state.settings.data_dir / "uploads" / f"{run_id}.json"
    original = sidecar.read_bytes()
    commit(client, upload(client, "ct", [{}]))
    with database(client.app.state.settings) as db:
        for candidate_id in (10, 11):
            db.execute(
                "INSERT INTO candidates(candidate_id,internal_ref,identity_status,"
                "created_at,updated_at) "
                "VALUES (?,?,'ambiguous','2026-01-01','2026-01-01')",
                (candidate_id, f"PFC-C-{candidate_id:06d}"),
            )
            db.execute(
                "INSERT INTO candidate_emails(candidate_id,email,email_key,source) "
                "VALUES (?,'taylor@example.com','taylor@example.com','CT')",
                (candidate_id,),
            )
    response = client.post(f"/api/v1/ingestion-runs/{run_id}/commit")
    assert response.status_code == 409
    assert response.json()["code"] == "AMBIGUOUS_IDENTITY"
    assert sidecar.read_bytes() == original
    assert not sidecar.with_suffix(".pending.json").exists()
    assert client.get(f"/api/v1/ingestion-runs/{run_id}").json() == pending
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM resources").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT rows_new FROM ingestion_runs WHERE ingestion_run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )
        assert verify_chain(db)


def test_req_ing_06_noop_and_failed_uploads_do_not_retain_workbooks(client: TestClient) -> None:
    payload = data("dl", [{}])
    first = client.post("/api/v1/uploads/dl", files={"file": ("dl.xlsx", payload)}).json()
    commit(client, first)
    directory = client.app.state.settings.data_dir / "uploads"
    retained = set(directory.iterdir())
    for _ in range(3):
        response = client.post("/api/v1/uploads/dl", files={"file": ("dl.xlsx", payload)})
        assert response.status_code == 200
        run = response.json()
        assert run["status"] == "no_op" and run["no_op"]
        assert not (directory / f"{run['ingestion_run_id']}.xlsx").exists()
        assert client.get(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}").json() == run
    workbook = Workbook()
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    for invalid in (b"not a workbook", output.getvalue()):
        response = client.post("/api/v1/uploads/dl", files={"file": ("bad.xlsx", invalid)})
        assert response.status_code == 422
        failed = client.get("/api/v1/ingestion-runs").json()["items"][0]
        assert failed["status"] == "failed"
        assert not (directory / f"{failed['ingestion_run_id']}.xlsx").exists()
        assert not (directory / f"{failed['ingestion_run_id']}.json").exists()
    assert set(directory.iterdir()) == retained
    assert (directory / f"{first['ingestion_run_id']}.xlsx").read_bytes() == payload
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM ingestion_runs").fetchone()[0] == 6
        assert (
            db.execute("SELECT count(*) FROM audit_log WHERE action='INGEST_NO_OP'").fetchone()[0]
            == 3
        )
        assert verify_chain(db)


def test_req_ing_03_snapshot_staging_failure_does_not_commit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = upload(client, "dl", [{}])
    run_id = run["ingestion_run_id"]
    sidecar = client.app.state.settings.data_dir / "uploads" / f"{run_id}.json"
    staged = service.repo.staged_snapshot_path(client.app.state.settings, run_id)
    original = sidecar.read_bytes()
    write_text = Path.write_text

    def fail_staging(path, text, **kwargs):
        if path == staged:
            write_text(path, "partial", **kwargs)
            raise OSError("staging unavailable")
        return write_text(path, text, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_staging)
    response = client.post(f"/api/v1/ingestion-runs/{run_id}/commit")
    assert response.status_code == 500
    assert not staged.exists()
    assert sidecar.read_bytes() == original
    assert client.get(f"/api/v1/ingestion-runs/{run_id}").json() == run
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM demands").fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM audit_log WHERE action='INGEST_COMMIT'").fetchone()


def test_req_ing_03_snapshot_promotion_failure_returns_durable_committed_review(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pending = upload(client, "dl", [{}])
    commit(client, upload(client, "dl", [{}]))
    run_id = pending["ingestion_run_id"]
    sidecar = client.app.state.settings.data_dir / "uploads" / f"{run_id}.json"
    original = sidecar.read_bytes()

    def fail_replace(source, destination):
        raise OSError("promotion unavailable")

    monkeypatch.setattr(service.os, "replace", fail_replace)
    result = commit(client, pending)
    assert result["status"] == "committed"
    assert result["counts"]["rows_new"] == 0 and result["counts"]["rows_unchanged"] == 1
    assert result["diff"]["new"] == [] and result["diff"]["unchanged"] == ["T11234"]
    assert sidecar.read_bytes() == original
    assert service.repo.staged_snapshot_path(client.app.state.settings, run_id).exists()
    assert "Snapshot promotion failed" in caplog.text
    assert client.get(f"/api/v1/ingestion-runs/{run_id}").json() == result
    assert client.get("/api/v1/ingestion-runs").json()["items"][1] == result
    assert client.post(f"/api/v1/ingestion-runs/{run_id}/commit").status_code == 409
    with database(client.app.state.settings) as db:
        assert (
            service.repo.run_response(db, client.app.state.settings, run_id).model_dump(mode="json")
            == result
        )
        assert verify_chain(db)


def test_req_in_24_replaced_original_cv_moves_old_provenance_to_identical_copy(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    original = resume_docx(root, "Alex original.docx", "alex@example.com SQL", 1767225600)
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        old = dict(db.execute("SELECT * FROM resumes").fetchone())
    copy = root / "Alex retained.docx"
    copy.write_bytes(original.read_bytes())
    os.utime(copy, (1767312000, 1767312000))
    resume_docx(root, original.name, "alex@example.com Python", 1767400000)
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == 1 and summary["unchanged"] == 1
    with database(client.app.state.settings) as db:
        rows = db.execute("SELECT * FROM resumes ORDER BY resume_id").fetchall()
        assert len(rows) == 2
        assert rows[0]["file_sha256"] == old["file_sha256"]
        assert rows[0]["source_path"] == copy.name
        assert rows[0]["file_mtime"] == "2026-01-02T00:00:00Z"
        assert rows[1]["source_path"] == original.name
        assert rows[1]["file_sha256"] != old["file_sha256"]
    history = client.get(f"/api/v1/candidates/{old['candidate_id']}/cv-versions").json()
    assert [(v["source_path"], v["is_latest"]) for v in history] == [
        (original.name, True),
        (copy.name, False),
    ]
