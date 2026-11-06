"""REQ-IN-21/23: real CV jobs, version backfill, provenance and masked API facts."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import commit, configure_resume_root, resume_docx, upload, wait_cv_job

from pfc.adapters.llm.providers import FakeProvider, GenerateResult, PromptRequest
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.domain.normalise.text import normalise_text

TEXT = (
    "Name: Alex Border\nPhone: +44 123456789\nalex@example.com\nWork Experience\n"
    "Engineer at Example\nEducation\nBSc Computing\nProjects\n"
    "Atlas: contact alex@example.com on +44 123456789"
)


def payload() -> str:
    return json.dumps(
        {
            "name": {"value": "Example", "evidence": "Engineer at Example"},
            "phone": None,
            "experience": [
                {"value": "Engineer", "evidence": "Engineer at Example"},
                {"value": "Fabricated CEO", "evidence": "Engineer at Example"},
                {"value": "CEO", "evidence": "Never present quote"},
            ],
            "qualifications": [],
            "projects": [{"value": "Invented project", "evidence": "Not in this CV"}],
        }
    )


@pytest.mark.parametrize("mode", ["absent", "healthy", "timeout", "invalid"])
def test_req_in_21_23_sync_persists_only_grounded_facts(
    client: TestClient,
    tmp_path: Path,
    mode: str,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", TEXT)
    settings = client.app.state.settings

    class Provider(FakeProvider):
        async def generate(
            self, request: PromptRequest, *, api_version: str | None = None
        ) -> GenerateResult:
            # REQ-IN-23: generation must never hold the CV persistence write lock.
            with database(settings) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            if mode == "timeout":
                await asyncio.sleep(10)
            return await super().generate(request, api_version=api_version)

    if mode != "absent":
        client.app.state.llm_provider = Provider("invalid JSON" if mode == "invalid" else payload())
    if mode == "timeout":
        with database(settings) as db:
            db.execute(
                "INSERT INTO settings(scope,key,value_json,updated_at) "
                "VALUES ('system','cv.llm_timeout_s','0.05','2026-11-06')"
            )
    result = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert result["new_versions"] == 1
    assert result["facts_llm_failed"] == int(mode == "invalid")
    assert result["facts_llm_timeout"] == int(mode == "timeout")
    with database(settings) as db:
        version = db.execute("SELECT * FROM cv_versions").fetchone()
        candidate = version["candidate_id"]
        saved = json.loads(version["facts_json"])
        assert saved["emails"] == ["alex@example.com", "alex@example.com"]
        assert saved["facts_schema"] == 1
        assert version["text_norm"] == normalise_text(TEXT)
        assert saved["facts"]["name"]["value"] == "Alex Border"
        assert saved["facts"]["phone"]["value"] == "+44 123456789"
        assert saved["facts"]["experience"][0]["value"] == (
            "Engineer" if mode == "healthy" else "Engineer at Example"
        )
        assert saved["facts"]["experience"][0]["source"] == ("llm" if mode == "healthy" else "rule")
        assert saved["facts"]["qualifications"][0]["value"] == "BSc Computing"
        assert saved["facts"]["qualifications"][0]["source"] == "rule"
        assert saved["facts"]["projects"][0]["value"] == (
            "Atlas: contact alex@example.com on +44 123456789"
        )
        assert saved["facts"]["projects"][0]["source"] == "rule"
        assert (
            "Fabricated" not in version["facts_json"]
            and "Never present" not in version["facts_json"]
        )
        assert version["extracted_by"] == (
            "llm:fake:fake-v1" if mode == "healthy" else "rule_based"
        )
        assert version["prompt_version"] == ("cv_extract_v1" if mode == "healthy" else None)
        for key, value in saved["facts"].items():
            for fact in (
                [value]
                if key in ("name", "phone") and value
                else []
                if key in ("name", "phone")
                else value
            ):
                assert version["text_norm"][slice(*fact["span"])] == fact["evidence"]
                assert fact["value"] in fact["evidence"]
        assert db.execute("SELECT phone FROM candidates").fetchone()[0] is None
        assert not db.execute("SELECT 1 FROM resource_skills WHERE source='CV'").fetchone()
        details = db.execute(
            "SELECT details_json FROM audit_log WHERE action='CV_SYNC'"
        ).fetchone()[0]
        assert "alex@example.com" not in details and "+44 123456789" not in details
        assert verify_chain(db)
    for endpoint in (
        f"/api/v1/candidates/{candidate}/cv-versions",
        f"/api/v1/candidates/{candidate}",
    ):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        items = (
            response.json() if isinstance(response.json(), list) else response.json()["cvVersions"]
        )
        assert items[0]["facts"]["phone"]["value"] == "***89"
        assert "alex@example.com" not in response.text and "+44 123456789" not in response.text
        assert items[0]["facts"]["phone"]["evidence"] == "***89"


def test_req_in_23_backfill_preserves_emails_and_version(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", TEXT)
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        version = db.execute("SELECT cv_version_id FROM cv_versions").fetchone()[0]
        db.execute(
            "UPDATE cv_versions SET facts_json=?,extracted_by='legacy',prompt_version=NULL",
            (json.dumps({"emails": ["MiXeD@example.com"], "legacy": True}),),
        )
    result = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert result["new_versions"] == 0 and result["unchanged"] == 1
    with database(client.app.state.settings) as db:
        rows = db.execute(
            "SELECT cv_version_id,facts_json,extracted_by FROM cv_versions"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] == version
        data: dict[str, Any] = json.loads(rows[0][1])
        assert data["emails"] == ["MiXeD@example.com"] and data["legacy"] is True
        assert data["facts_schema"] == 1 and data["facts"]["name"]["value"] == "Alex Border"
        assert rows[0][2] == "rule_based"
    client.app.state.llm_provider = FakeProvider("invalid JSON")
    repeat = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeat["unchanged"] == 1 and repeat["facts_llm_failed"] == 0


@pytest.mark.parametrize(
    "line,phone_value",
    [
        ("+44 2020-2024 555", None),
        ("+44 7700 900123 2020-2024", "+44 7700 900123"),
        ("2020-2024 +44 7700 900123", "+44 7700 900123"),
        ("+44 2015-2019 555", None),
        ("(020) 2020 2024", "(020) 2020 2024"),
        ("2015-2019 +44 20 7946 0958", "+44 20 7946 0958"),
    ],
)
def test_req_in_23_adjacent_phone_and_years_masked_in_both_responses(
    client: TestClient,
    tmp_path: Path,
    line: str,
    phone_value: str | None,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", f"alex@example.com\nProjects\n{line}")
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        version = db.execute("SELECT candidate_id,text_norm,facts_json FROM cv_versions").fetchone()
        phone = json.loads(version["facts_json"])["facts"]["phone"]
        if phone_value is None:
            assert phone is None
        else:
            assert phone["value"] == phone_value
            assert version["text_norm"][slice(*phone["span"])] == phone["evidence"]
        candidate = version["candidate_id"]
    for endpoint in (
        f"/api/v1/candidates/{candidate}/cv-versions",
        f"/api/v1/candidates/{candidate}",
    ):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        assert "7700" not in response.text and "900123" not in response.text
        items = (
            response.json() if isinstance(response.json(), list) else response.json()["cvVersions"]
        )
        if phone_value is None:
            assert items[0]["facts"]["phone"] is None
        else:
            assert items[0]["facts"]["phone"]["value"] == "***" + phone_value[-2:]
        project = items[0]["facts"]["projects"][0]
        assert project["evidence"] == project["value"] == "***" + line[-2:]


def test_req_in_23_plain_year_range_preserved_in_both_responses(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "alex@example.com\nExperience\n2015-2019 at Acme")
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        candidate = db.execute("SELECT candidate_id FROM cv_versions").fetchone()[0]
    for endpoint in (
        f"/api/v1/candidates/{candidate}/cv-versions",
        f"/api/v1/candidates/{candidate}",
    ):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        items = (
            response.json() if isinstance(response.json(), list) else response.json()["cvVersions"]
        )
        assert items[0]["facts"]["phone"] is None
        fact = items[0]["facts"]["experience"][0]
        assert fact["evidence"] == fact["value"] == "2015-2019 at Acme"


@pytest.mark.parametrize("invalid", [False, True])
def test_req_in_21_identical_normalized_cv_text_uses_one_model_call(
    client: TestClient,
    tmp_path: Path,
    invalid: bool,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "First.docx", TEXT)
    resume_docx(root, "Second.docx", TEXT.replace("Engineer at", "Engineer  at"))

    class Provider(FakeProvider):
        calls = 0

        async def generate(
            self,
            request: PromptRequest,
            *,
            api_version: str | None = None,
        ) -> GenerateResult:
            self.calls += 1
            return await super().generate(request, api_version=api_version)

    provider = Provider("invalid JSON" if invalid else payload())
    client.app.state.llm_provider = provider
    summary = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert summary["new_versions"] == 2 and provider.calls == 1
    assert summary["facts_llm_failed"] == (2 if invalid else 0)
    with database(client.app.state.settings) as db:
        rows = db.execute("SELECT text_sha256,facts_json FROM cv_versions").fetchall()
        assert len(rows) == 2 and rows[0][0] == rows[1][0]
        assert rows[0][1] == rows[1][1]
    repeat = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert repeat["unchanged"] == 2 and provider.calls == 1
