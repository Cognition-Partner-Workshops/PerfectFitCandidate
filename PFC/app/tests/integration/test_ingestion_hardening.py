"""Phase 1f ingestion limits and confinement regressions."""

import json
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import commit, configure_resume_root, data, resume_docx, upload, wait_cv_job

from pfc.db.connection import database
from pfc.services.ingestion import cv_sync
from pfc.services.ingestion.limits import WorkbookLimits, positive_setting
from pfc.services.ingestion.models import file_sha256


def setting(client: TestClient, key: str, value: object, scope: str = "system") -> None:
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(scope,user_id,key,value_json,updated_at) "
            "VALUES (?,?,?,?, '2026-01-01')",
            (scope, None if scope == "system" else 1, key, json.dumps(value)),
        )


def zip_payload(entries: dict[str, bytes]) -> bytes:
    result = BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return result.getvalue()


@pytest.mark.parametrize(
    "limit,value,payload,code,status",
    [
        ("ingestion.max_upload_mb", 0.001, data("dl", [{}]), "UPLOAD_TOO_LARGE", 413),
        ("ingestion.max_rows", 1, data("dl", [{}, {}]), "WORKBOOK_TOO_MANY_ROWS", 422),
        (
            "ingestion.max_sheets",
            1,
            zip_payload(
                {
                    "xl/workbook.xml": (
                        b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                        b'<sheets><sheet name="A"/><sheet name="B"/></sheets></workbook>'
                    )
                }
            ),
            "WORKBOOK_TOO_MANY_SHEETS",
            422,
        ),
        ("ingestion.max_uncompressed_mb", 0.001, data("dl", [{}]), "WORKBOOK_ZIP_BOMB", 422),
        (
            "unused",
            1,
            zip_payload({"large.bin": b"x" * (1024 * 1024 + 1)}),
            "WORKBOOK_ZIP_BOMB",
            422,
        ),
        ("unused", 1, b"not a zip", "WORKBOOK_INVALID", 422),
        ("unused", 1, zip_payload({"xl/workbook.xml": b"<broken>"}), "WORKBOOK_INVALID", 422),
        ("unused", 1, zip_payload({"xl/workbook.xml": b"<workbook/>"}), "WORKBOOK_INVALID", 422),
    ],
)
def test_workbook_limits_leave_failed_history_without_files(
    client: TestClient, limit: str, value: object, payload: bytes, code: str, status: int
) -> None:
    setting(client, limit, value)
    response = client.post("/api/v1/uploads/dl", files={"file": ("test.xlsx", payload)})
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.headers["content-type"] == "application/problem+json"
    run = client.get("/api/v1/ingestion-runs").json()["items"][0]
    assert run["status"] == "failed"
    assert not list((client.app.state.settings.data_dir / "uploads").iterdir())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE root SYSTEM "file:///nonexistent">',
        '<!DOCTYPE root [<!ENTITY data "blocked">]>',
    ],
)
def test_workbook_rejects_xml_declarations_before_openpyxl(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, encoding: str, declaration: str
) -> None:
    from pfc.services.ingestion import workbooks

    def must_not_open(*args, **kwargs):
        pytest.fail("openpyxl must not see unsafe XML")

    monkeypatch.setattr(workbooks, "load_workbook", must_not_open)
    payload = zip_payload(
        {
            "xl/workbook.xml": b"<workbook/>",
            "xl/worksheets/sheet1.xml": (declaration + "<root/>").encode(encoding),
        }
    )
    response = client.post("/api/v1/uploads/dl", files={"file": ("test.xlsx", payload)})
    assert response.status_code == 422
    assert response.json()["code"] == "WORKBOOK_INVALID"


@pytest.mark.parametrize("value", [None, True, "2", 0, -1, [], {}, float("inf"), 1e308])
def test_invalid_system_limits_use_defaults(client: TestClient, value: object) -> None:
    # JSON storage rejects Infinity, so use a valid JSON numeric exponent instead.
    if value == float("inf"):
        with database(client.app.state.settings) as db:
            db.execute(
                "INSERT INTO settings(scope,key,value_json,updated_at) "
                "VALUES ('system','ingestion.max_rows','1e999','2026-01-01')"
            )
    else:
        setting(client, "ingestion.max_rows", value)
    setting(client, "ingestion.max_rows", 1, scope="user")
    with database(client.app.state.settings) as db:
        assert WorkbookLimits.read(db).max_rows == 20000
        assert positive_setting(db, "missing", 20) == 20
    run = upload(client, "dl", [{}, {"Code Requisition": "T11235"}])
    assert run["counts"]["rows_read"] == 2
    assert commit(client, run)["status"] == "committed"


def test_workbook_row_limit_boundary_and_commit_recheck(client: TestClient) -> None:
    setting(client, "ingestion.max_rows", 2)
    run = upload(client, "dl", [{}, {"Code Requisition": "T11235"}])
    assert run["counts"]["rows_read"] == 2
    setting(client, "ingestion.max_rows", 1)
    response = client.post(f"/api/v1/ingestion-runs/{run['ingestion_run_id']}/commit")
    assert response.status_code == 422
    assert response.json()["code"] == "WORKBOOK_TOO_MANY_ROWS"
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM demands").fetchone()[0] == 0


def test_openpyxl_defusedxml_is_active() -> None:
    from openpyxl import xml
    from openpyxl.xml.functions import iterparse

    assert xml.DEFUSEDXML is True
    assert iterparse.__module__ == "defusedxml.common"


@pytest.mark.parametrize("stored", ["../outside.docx", "/outside.docx", "C:/outside.docx"])
def test_unsafe_provenance_is_not_opened(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    path = resume_docx(root, "Alex.docx", "alex@example.com SQL")
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    (tmp_path / "outside.docx").write_bytes(path.read_bytes())
    with database(client.app.state.settings) as db:
        db.execute("UPDATE resumes SET source_path=?", (stored,))

    def checked_hash(path: Path) -> str:
        assert path.resolve().is_relative_to(root.resolve()), path
        return file_sha256(path)

    monkeypatch.setattr(cv_sync, "file_sha256", checked_hash)
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == 0
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT source_path FROM resumes").fetchone()[0] == "Alex.docx"
