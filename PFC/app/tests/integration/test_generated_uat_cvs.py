"""The generator's CVs go through the real CT ingest and CV sync (UAT runbook step 3)."""

from pathlib import Path

from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import commit, configure_resume_root, wait_cv_job

from pfc.db.connection import database
from scripts.generate_uat_data import generate, phone


def test_generated_cvs_sync_and_link_to_their_ct_candidates(
    client: TestClient, tmp_path: Path
) -> None:
    paths = generate(tmp_path / "uat", demands=5, resources=5)
    response = client.post(
        "/api/v1/uploads/ct", files={"file": ("CT.xlsx", paths["ct"].read_bytes())}
    )
    assert response.status_code == 200, response.text
    commit(client, response.json())
    configure_resume_root(client, paths["resumes"])

    result = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert result["scanned"] == result["new_versions"] == 5
    for skipped in (
        "missing_cv",
        "ambiguous",
        "unmatched",
        "extraction_failed",
        "extraction_timeout",
        "too_large",
        "warnings",
    ):
        assert result[skipped] == 0, skipped

    with database(client.app.state.settings) as db:
        rows = db.execute(
            "SELECT c.candidate_id, r.email_address, c.identity_status, v.identity_match, "
            "s.source_path FROM cv_versions v JOIN resumes s USING(resume_id) "
            "JOIN candidates c ON c.candidate_id=v.candidate_id "
            "JOIN resources r ON r.candidate_id=c.candidate_id ORDER BY s.source_path"
        ).fetchall()
    assert len(rows) == 5
    for index, row in enumerate(rows, start=1):
        assert row["source_path"] == f"{row['email_address']}.docx"
        assert row["identity_status"] == "complete"
        assert row["identity_match"] == "email_exact"
        versions = client.get(f"/api/v1/candidates/{row['candidate_id']}/cv-versions")
        assert versions.status_code == 200, versions.text
        assert versions.json()[0]["facts"]["phone"]["value"] == "***" + phone(index)[-2:]
        assert phone(index) not in versions.text
