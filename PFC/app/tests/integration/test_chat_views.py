"""Safe chat views over production CT ingestion and CV versioning."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ingestion import client as client
from test_ingestion import commit, configure_resume_root, resume_docx, upload, wait_cv_job
from test_matching_service import seed_demand, seed_resource

from pfc.db.connection import database
from pfc.services.chat_intent import Lookup
from pfc.services.chat_sql import execute_query
from pfc.services.matching.current import read_current_runs


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


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "not JSON",
        "[]",
        "null",
        '{"demands":[]}',
        '{"demands":"wrong"}',
        '{"demands":{"bad":{}," ":{},"+":{}}}',
    ],
)
def test_chat_match_results_current_run_parity(client: TestClient, malformed: str | None) -> None:
    with database(client.app.state.settings) as db:
        demands = [seed_demand(db, f"T{12000 + i}") for i in range(6)]
        empty, first, second, unassessed, closed, archived = demands
        resource = seed_resource(db, "SYNTHETIC_RESOURCE")
        db.execute("UPDATE demands SET lifecycle='closed' WHERE demand_id=?", (closed,))
        db.execute("UPDATE demands SET lifecycle='archived' WHERE demand_id=?", (archived,))
        template = db.execute("SELECT template_id FROM scoring_templates LIMIT 1").fetchone()[0]
        model = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version) "
            "VALUES ('parity','onnx','parity',2,'parity')"
        ).lastrowid

        def add_run(status: str, summary: str | None, matched: list[int]) -> int:
            cursor = db.execute(
                "INSERT INTO match_runs(scope,template_id,embedding_model_id,engine_version,"
                "synonyms_version,config_hash,status,dq_summary_json) "
                "VALUES ('full',?,?,'parity','parity','parity',?,?)",
                (template, model, status, summary),
            )
            assert cursor.lastrowid is not None
            run = cursor.lastrowid
            for demand in matched:
                db.execute(
                    "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                    "resource_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
                    "matching_evidence,gaps_checks,eligibility_evidence) "
                    "SELECT ?,d.demand_id,d.current_version_id,q.jd_req_id,?,?,'Strong',1,1,"
                    "'exact','Aligned','{}','[]','[]' FROM demands d "
                    "JOIN jd_requirements q ON q.demand_version_id=d.current_version_id "
                    "WHERE d.demand_id=?",
                    (run, resource, run * 10.0, demand),
                )
            return run

        def summary(ids: list[int]) -> str:
            return json.dumps({"demands": {str(id): {} for id in ids}})

        add_run(
            "completed",
            summary([empty, first, second, closed, archived]),
            [empty, first, second, closed, archived],
        )
        first_run = add_run("completed", summary([first]), [first])
        second_run = add_run("completed", malformed, [second])
        empty_run = add_run("completed", summary([empty]), [])
        for status in ("failed", "running"):
            add_run(status, summary([empty, first, second]), [empty, first, second])
        add_run("completed", malformed, [])

        current = read_current_runs(db)
        assert current == {empty: empty_run, first: first_run, second: second_run}
        open_demands = dict(
            db.execute("SELECT demand_id,code_requisition FROM demands WHERE lifecycle='open'")
        )
        expected = {}
        actual = {}
        implied_runs = {}
        for demand, requisition in open_demands.items():
            expected[demand] = {
                tuple(row)
                for row in db.execute(
                    "SELECT d.code_requisition,'Resource #'||m.resource_id,m.rank,m.score,m.band,"
                    "m.mandatory_missing,m.shortlisted FROM match_results m "
                    "JOIN demands d USING(demand_id) JOIN resources r USING(resource_id) "
                    "WHERE m.demand_id=? AND m.run_id=? AND r.lifecycle!='archived'",
                    (demand, current.get(demand)),
                )
            }
            actual[demand] = {
                tuple(row)
                for row in db.execute(
                    "SELECT * FROM chat_v_match_results WHERE requisition=?",
                    (requisition,),
                )
            }
            if actual[demand]:
                runs = {
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT run_id FROM match_results WHERE demand_id=? AND score IN "
                        "(SELECT score FROM chat_v_match_results WHERE requisition=?)",
                        (demand, requisition),
                    )
                }
                assert runs == {current[demand]}
                implied_runs[demand] = runs.pop()
        assert actual == expected
        assert actual[empty] == actual[unassessed] == set()
        assert implied_runs == {id: run for id, run in current.items() if expected[id]}
        assert {
            row[0] for row in db.execute("SELECT DISTINCT requisition FROM chat_v_match_results")
        } == {open_demands[first], open_demands[second]}
