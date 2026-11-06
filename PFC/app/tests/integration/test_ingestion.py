"""REQ-ING-01..03/06: real sessions, SQLite transactions and synthetic workbooks."""

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.ingestion import service
from pfc.services.ingestion.repository import parse
from pfc.services.ingestion.workbooks import CT, DL

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
        config = load_domain_config()
        with database(settings) as db:
            db.executemany(
                "INSERT INTO grades(code,ordinal) VALUES (?,?)",
                [(code, i) for i, code in enumerate(config.grades.codes, 1)],
            )
            for status in config.bench_statuses.statuses.values():
                fields = status.model_dump()
                db.execute(
                    f"INSERT INTO bench_statuses({','.join(fields)}) VALUES "
                    f"({','.join('?' for _ in fields)})",
                    tuple(fields.values()),
                )
            for source, code in config.bench_statuses.status_map.items():
                status = config.bench_statuses.statuses[code]
                db.execute(
                    "INSERT INTO status_map(source_value,canonical_code,priority,"
                    "is_excluded,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (source, code, status.priority or 2, status.is_excluded, "2026-01-01"),
                )
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
