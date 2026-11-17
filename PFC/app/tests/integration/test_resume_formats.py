import json

import pytest
from test_ingestion import client as client
from test_ingestion import commit, configure_resume_root, resume_docx, upload, wait_cv_job

from pfc.db.connection import database
from pfc.services.ingestion import cv_sync


def store(client, key, value, scope="system"):
    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(scope,user_id,key,value_json,updated_at) "
            "VALUES (?,?,?,?, 'now')",
            (scope, 1 if scope == "user" else None, key, json.dumps(value)),
        )


def test_settings_default_normalization_and_readonly(client):
    response = client.get("/api/v1/settings/me")
    assert response.status_code == 200
    assert response.json()["resumeFileTypes"] == [".docx", ".pdf"]
    assert isinstance(response.json()["resume_converter_available"], bool)
    response = client.put(
        "/api/v1/settings/me", json={"resumeFileTypes": [" DOCX ", ".PDF", "pdf", "RTF"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["resumeFileTypes"] == [".docx", ".pdf", ".rtf"]
    with database(client.app.state.settings) as db:
        assert json.loads(
            db.execute("SELECT value_json FROM settings WHERE key='resume.file_types'").fetchone()[
                0
            ]
        ) == [".docx", ".pdf", ".rtf"]
    response = client.put("/api/v1/settings/me", json={"resume_file_types": ["txt"]})
    assert response.status_code == 200
    assert response.json()["resumeFileTypes"] == [".txt"]
    response = client.put("/api/v1/settings/me", json={"resume_converter_available": False})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("value", [[], [".exe"], ["pdf", "bogus"], None, "pdf", [4]])
def test_settings_invalid_formats(client, value):
    response = client.put("/api/v1/settings/me", json={"resumeFileTypes": value})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_format_scope_precedence_and_prepare(client, tmp_path):
    root = tmp_path / "system"
    root.mkdir()
    user_root = tmp_path / "user"
    user_root.mkdir()
    store(client, "resume.root_path", str(root))
    store(client, "resume.file_types", [".rtf"])
    response = client.get("/api/v1/settings/me").json()
    assert response["resume_root_path"] == str(root)
    assert response["resumeFileTypes"] == [".rtf"]
    assert cv_sync.prepare(client.app.state.settings, 1, []) == (root, [], {".rtf"})
    store(client, "resume.root_path", str(user_root), "user")
    store(client, "resume.file_types", [".txt"], "user")
    assert cv_sync.prepare(client.app.state.settings, 1, []) == (user_root, [], {".txt"})
    assert cv_sync.prepare(client.app.state.settings, 2, []) == (root, [], {".rtf"})


def test_sync_filter_reasons_converter_and_untick_preserves_versions(client, tmp_path, monkeypatch):
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "alex@example.com SQL")
    (root / "Alex.rtf").write_text(r"{\rtf1 alex@example.com Python}")
    (root / "Alex.txt").write_text("alex@example.com Text")
    (root / "Alex.doc").write_bytes(b"legacy")
    (root / "Alex.ppt").write_bytes(b"legacy slides")
    (root / "Alex.unknown").write_text("unknown format")
    store(client, "resume.file_types", [".docx", ".rtf", ".doc", ".ppt"])
    monkeypatch.setenv("PFC_SOFFICE", str(tmp_path / "missing"))
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == 2
    assert summary["scanned"] == 6
    assert summary["skipped_count"] == 4
    assert summary["extraction_failed"] == 2
    assert summary["skipped_files"] == [
        {"file_name": "Alex.doc", "reason": "converter unavailable"},
        {"file_name": "Alex.ppt", "reason": "converter unavailable"},
        {"file_name": "Alex.txt", "reason": "format not selected"},
        {"file_name": "Alex.unknown", "reason": "format not supported"},
    ]
    with database(client.app.state.settings) as db:
        before = [
            tuple(row)
            for row in db.execute(
                "SELECT cv_version_id,resume_id,is_latest,version_no "
                "FROM cv_versions ORDER BY cv_version_id"
            )
        ]
        assert len(before) == 2
        assert {row[0] for row in db.execute("SELECT mime_type FROM resumes")} == {
            "application/rtf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    store(client, "resume.file_types", [".pdf"])
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == summary["unchanged"] == 0
    assert summary["skipped_count"] == 6
    with database(client.app.state.settings) as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT cv_version_id,resume_id,is_latest,version_no "
                "FROM cv_versions ORDER BY cv_version_id"
            )
        ] == before
