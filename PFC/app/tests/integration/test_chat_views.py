"""Safe chat views over production CT ingestion and CV versioning."""

from pathlib import Path

from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import commit, configure_resume_root, resume_docx, upload, wait_cv_job

from pfc.db.connection import database
from pfc.services.chat_intent import Lookup
from pfc.services.chat_sql import execute_query


def test_ct_skills_capacity_and_dictionary_changes(client: TestClient) -> None:
    settings = client.app.state.settings
    with database(settings) as db:
        sql = db.execute("SELECT skill_id FROM skills WHERE name='SQL'").fetchone()[0]
        db.execute(
            "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,status,updated_at) "
            "VALUES (?,'PFC alias','pfc alias','equivalent','approved','2026-11-06')",
            (sql,),
        )
    commit(
        client,
        upload(
            client,
            "ct",
            [
                {"Skills": "PFC alias; SQL; alex@example.com; Unknown", "Available Capacity": "Y"},
                {
                    "ShortName": "Synthetic Other",
                    "Email Address": "other@example.invalid",
                    "Skills": "Python",
                    "Available Capacity": "N",
                },
                {
                    "ShortName": "Synthetic Missing",
                    "Email Address": "missing@example.invalid",
                    "Skills": "",
                    "Available Capacity": None,
                },
            ],
        ),
    )
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM resource_skills").fetchone()[0] == 0
        assert [
            row[0]
            for row in db.execute("SELECT capacity FROM chat_v_resources ORDER BY resource_ref")
        ] == ["Y", "N", None]
        answer = execute_query(
            db,
            Lookup(
                intent="query",
                sql="SELECT resource_ref,skill,source,years "
                "FROM chat_v_resource_skills ORDER BY resource_ref,skill",
                explanation="This lists canonical skills.",
            ),
        )
        assert answer.query and answer.query.rows == [
            ["Resource #1", "SQL", "CT", None],
            ["Resource #2", "Python", "CT", None],
        ]
    with database(settings) as db:
        db.execute("UPDATE skill_synonyms SET status='rejected' WHERE term='PFC alias'")
        db.execute("UPDATE resources SET skills_raw='PFC alias' WHERE resource_id=1")
        db.execute("UPDATE skills SET is_active=0 WHERE name='Python'")
    with database(settings) as db:
        answer = execute_query(
            db,
            Lookup(
                intent="query",
                sql="SELECT * FROM chat_v_resource_skills",
                explanation="This lists canonical skills.",
            ),
        )
        assert answer.query and answer.query.rows == []
    with database(settings) as db:
        db.execute("UPDATE resources SET available_capacity='unknown' WHERE resource_id=1")
        assert (
            db.execute(
                "SELECT capacity FROM chat_v_resources WHERE resource_ref='Resource #1'"
            ).fetchone()[0]
            is None
        )


def test_cv_skills_only_latest_live_version(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "resumes"
    root.mkdir()
    configure_resume_root(client, root)
    commit(client, upload(client, "ct", [{"Skills": ""}]))
    resume_docx(root, "Alex.docx", "alex@example.com\nSkills\nSQL; Python")
    assert wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))["new_versions"] == 1
    resume_docx(root, "Alex.docx", "alex@example.com\nSkills\nSQL; AWS")
    assert wait_cv_job(client, client.post("/api/v1/resources/cv-sync"))["new_versions"] == 1
    settings = client.app.state.settings
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM cv_versions").fetchone()[0] == 2
        assert (
            db.execute("SELECT count(*) FROM resource_skills WHERE skill_raw='SQL'").fetchone()[0]
            == 2
        )
        assert (
            db.execute("SELECT count(*) FROM resource_skills WHERE skill_raw='Python'").fetchone()[
                0
            ]
            == 1
        )
        candidate = db.execute("SELECT candidate_id FROM resources").fetchone()[0]
        answer = execute_query(
            db,
            Lookup(
                intent="query",
                sql="SELECT skill,source FROM chat_v_resource_skills ORDER BY skill",
                explanation="This lists current CV skills.",
            ),
        )
        assert answer.query and answer.query.rows == [["AWS", "CV"], ["SQL", "CV"]]
    with database(settings) as db:
        db.execute("UPDATE cv_versions SET extraction_removed_at='2026-11-06' WHERE is_latest=1")
        assert db.execute("SELECT count(*) FROM resource_skills").fetchone()[0] > 0
    with database(settings) as db:
        answer = execute_query(
            db,
            Lookup(
                intent="query",
                sql="SELECT * FROM chat_v_resource_skills",
                explanation="This lists current CV skills.",
            ),
        )
        assert answer.query and answer.query.rows == []
    with database(settings) as db:
        db.execute("UPDATE cv_versions SET extraction_removed_at=NULL WHERE is_latest=1")
    response = client.delete(f"/api/v1/occ/candidates/{candidate}/cv-extractions")
    assert response.status_code == 200, response.text
    with database(settings) as db:
        answer = execute_query(
            db,
            Lookup(
                intent="query",
                sql="SELECT * FROM chat_v_resource_skills",
                explanation="This lists current CV skills.",
            ),
        )
        assert answer.query and answer.query.rows == []
