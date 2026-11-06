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
from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.domain.normalise.text import normalise_text
from pfc.services.ingestion import cv_sync
from pfc.services.ingestion.cv_facts import ExtractedFacts, rule_facts

TEXT = (
    "Name: Alex Border\nPhone: +44 123456789\nalex@example.com\nWork Experience\n"
    "Engineer at Example\nEducation\nBSc Computing\nProjects\n"
    "Atlas: contact alex@example.com on +44 123456789\nSkills\nSQL; Unlisted"
)


def test_req_in_23_nameless_cv_without_ct_link_keeps_profile_incomplete(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    resume_docx(root, "known.docx", "Experience\nEngineer\nSkills\nSQL")
    settings = client.app.state.settings
    with database(settings) as db:
        candidate = db.execute(
            "INSERT INTO candidates(internal_ref,identity_status,created_at,updated_at) "
            "VALUES ('PFC-C-STANDALONE','incomplete','2026-11-06','2026-11-06')"
        ).lastrowid
    source = ResumeFolderSource(root, work_dir=tmp_path / "extraction")
    result = source.scan(
        [CandidateLookup(candidate_id=str(candidate), source_paths=["known.docx"])]
    )
    assert len(result[0].versions) == 1
    version = result[0].versions[0]
    assert version.identity_match == "manual"
    facts = rule_facts(version.text_norm)
    assert facts.name is None
    with database(settings) as db:
        cv_sync._persist(db, candidate, version, "2026-11-06", root, ExtractedFacts(facts))
        assert not db.execute(
            "SELECT 1 FROM resources WHERE candidate_id=?", (candidate,)
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM candidate_emails WHERE candidate_id=?", (candidate,)
        ).fetchone()
    response = client.get(f"/api/v1/candidates/{candidate}")
    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["identityStatus"] == profile["resource"]["identityStatus"] == "incomplete"
    assert profile["resource"]["name"] is None and profile["resource"]["resourceId"] == 0
    assert profile["cvVersions"][0]["facts"]["name"] is None
    assert profile["ctEmailOptions"] == []


def test_req_in_21_23_skills_use_one_approved_snapshot(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", "alex@example.com\nKey Skills\nPFC alias, Unknown, Pending")
    resume_docx(root, "Other.docx", "alex@example.com\nTechnical Skills\nPFC alias, Unknown")
    settings = client.app.state.settings
    with database(settings) as db:
        skill_id = db.execute(
            "INSERT INTO skills(name,created_by) VALUES ('PFC Canonical','admin')"
        ).lastrowid
        for term, status in [("PFC alias", "approved"), ("Pending", "suggested")]:
            db.execute(
                "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,status,updated_at) "
                "VALUES (?,?,?,'equivalent',?,'2026-11-06')",
                (skill_id, term, term.casefold(), status),
            )
    calls = 0
    original = cv_sync.valid_synonym_table

    def snapshot(db: Any) -> Any:
        nonlocal calls
        assert not db.in_transaction
        calls += 1
        return original(db)

    monkeypatch.setattr(cv_sync, "valid_synonym_table", snapshot)
    result = wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    assert result["new_versions"] == 2 and calls == 1
    with database(settings) as db:
        for row in db.execute("SELECT text_norm,facts_json,candidate_id FROM cv_versions"):
            data = json.loads(row["facts_json"])
            assert data["facts_schema"] == 2
            skills = data["facts"]["skills"]
            assert skills[0]["canonical"] == "PFC Canonical"
            assert all(s["canonical"] is None for s in skills[1:])
            for s in skills:
                assert row["text_norm"][slice(*s["span"])] == s["value"] == s["evidence"]
            response = client.get(f"/api/v1/candidates/{row['candidate_id']}")
            assert response.status_code == 200, response.text
            facts = response.json()["cvVersions"][0]["facts"]
            assert facts["warnings"][0]["code"] == "PHONE_NOT_FOUND"
        assert not db.execute("SELECT 1 FROM resource_skills").fetchone()
        assert db.execute("SELECT phone FROM candidates").fetchone()[0] is None


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
            "skills": [],
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
        assert saved["facts_schema"] == 2
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
        assert version["prompt_version"] == ("cv_extract_v2" if mode == "healthy" else None)
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


@pytest.mark.parametrize("schema", [0, 1])
def test_req_in_23_backfill_preserves_emails_and_version(
    client: TestClient, tmp_path: Path, schema: int
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
            (
                json.dumps(
                    {
                        "emails": ["MiXeD@example.com"],
                        "legacy": True,
                        **({"facts_schema": schema, "facts": {}} if schema else {}),
                    }
                ),
            ),
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
        assert data["facts_schema"] == 2 and data["facts"]["name"]["value"] == "Alex Border"
        assert [s["value"] for s in data["facts"]["skills"]] == ["SQL", "Unlisted"]
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
        ("2015-2019 / 2020-2024 +44 20 7946 0958", "+44 20 7946 0958"),
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


@pytest.mark.parametrize(
    "dates", ["2015-2019", "2015-2019 / 2020-2024", "2015 - 2019, 2020 - 2024"]
)
def test_req_in_23_plain_year_range_preserved_in_both_responses(
    client: TestClient,
    tmp_path: Path,
    dates: str,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    resume_docx(root, "Alex.docx", f"alex@example.com\nExperience\n{dates} at Acme")
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
        assert fact["evidence"] == fact["value"] == f"{dates} at Acme"


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


def test_req_in_23_legacy_year_like_phone_masked_in_both_responses(
    client: TestClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{}]))
    text = (
        "Name: Alex Border\n2020 2024\nalex@example.com\nExperience\n2020 2024 at Acme\n"
        "Education\nCourse 2020 2024\nProjects\nWork 2020 2024"
    )
    resume_docx(root, "Alex.docx", text)
    wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))
    with database(client.app.state.settings) as db:
        row = db.execute("SELECT candidate_id,text_norm,facts_json FROM cv_versions").fetchone()
        saved = json.loads(row["facts_json"])
        assert saved["facts"]["phone"] is None
        value = "2020 2024"
        start = row["text_norm"].index(value)
        saved["facts"]["phone"] = {
            "value": value,
            "evidence": value,
            "span": [start, start + len(value)],
            "source": "rule",
        }
        db.execute("UPDATE cv_versions SET facts_json=?", (json.dumps(saved),))
        candidate = row["candidate_id"]
    for endpoint in (
        f"/api/v1/candidates/{candidate}/cv-versions",
        f"/api/v1/candidates/{candidate}",
    ):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        assert value not in response.text
        items = (
            response.json() if isinstance(response.json(), list) else response.json()["cvVersions"]
        )
        facts = items[0]["facts"]
        assert facts["phone"]["value"] == facts["phone"]["evidence"] == "***24"
        for category in ("experience", "qualifications", "projects"):
            assert "***24" in facts[category][0]["evidence"]
    with database(client.app.state.settings) as db:
        assert json.loads(db.execute("SELECT facts_json FROM cv_versions").fetchone()[0]) == saved
