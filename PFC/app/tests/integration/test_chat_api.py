"""Offline reference questions (P2-EX-05) through the production job and API."""

import asyncio
import json
import threading
from dataclasses import replace
from typing import Any

import pytest
from test_matching_api import PASSWORD, Api
from test_matching_api import api as api
from test_matching_service import (
    NOW,
    STAMP,
    seed_candidate,
    seed_cv,
    seed_demand,
    seed_resource,
    wait_done,
)

from pfc.adapters.llm.providers import (
    Capabilities,
    FakeProvider,
    GenerateResult,
    Health,
    PromptRequest,
)
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.db.users import create_user
from pfc.security.passwords import hasher
from pfc.services.chat_intent import Lookup


class ChatStub(FakeProvider):
    def __init__(self, *, mode: str = "local") -> None:
        super().__init__()
        self.mode = mode
        self.requests: list[PromptRequest] = []
        self.entered = threading.Event()
        self.lookup: dict[str, Any] = {}

    def capabilities(self) -> Capabilities:
        if self.mode == "external":
            return replace(super().capabilities(), data_residency="external")
        return super().capabilities()

    async def health(self) -> Health:
        return Health("down" if self.mode == "down" else "ok", "offline-chat-stub")

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        self.requests.append(request)
        if self.mode == "slow":
            self.entered.set()
            await asyncio.sleep(60)
        return GenerateResult(json.dumps(self.lookup), "fake", "offline-chat-stub")


def test_query_correction_and_archived_views(api: Api) -> None:
    _seed(api)
    client, _ = api

    class CorrectingStub(ChatStub):
        async def generate(
            self, request: PromptRequest, *, api_version: str | None = None
        ) -> GenerateResult:
            self.lookup = {
                "intent": "query",
                "sql": "SELECT * FROM resources"
                if not self.requests
                else "SELECT * FROM chat_v_match_results WHERE requisition='T10001'",
                "explanation": "This lists the stored ranking.",
            }
            return await super().generate(request, api_version=api_version)

    stub = CorrectingStub()
    client.app.state.llm_provider = stub
    answer = _answer(api, "Which candidate is best fit for T10001?")["answer"]
    assert answer["outcome"] == "answered"
    assert answer["query"]["ranking"]
    assert answer["query"]["rows"][0][3] == 84.5
    assert "human review required" in answer["text"]
    assert len(stub.requests) == 2
    assert "SQL rejected" in stub.requests[1].messages[-1]["content"]
    with database(client.app.state.settings) as db:
        for view in ("demands", "demand_skills", "match_results"):
            assert not db.execute(
                f"SELECT * FROM chat_v_{view} WHERE requisition='T10004'"
            ).fetchall()
        archived = db.execute(
            "SELECT resource_id FROM resources WHERE lifecycle='archived'"
        ).fetchone()[0]
        for view in ("resources", "resource_skills", "match_results"):
            assert not db.execute(
                f"SELECT * FROM chat_v_{view} WHERE resource_ref=?", (f"Resource #{archived}",)
            ).fetchall()
        details = json.loads(
            db.execute("SELECT details_json FROM audit_log WHERE action='CHAT_ANSWER'").fetchone()[
                0
            ]
        )
        assert details["row_count"] == 1 and len(details["sql_sha256"]) == 64
        newest = db.execute("SELECT max(run_id) FROM match_runs").fetchone()[0]
        demand = db.execute(
            "SELECT demand_id FROM demands WHERE code_requisition='T10001'"
        ).fetchone()[0]
        db.execute(
            "UPDATE match_runs SET dq_summary_json=? WHERE run_id=?",
            (json.dumps({"demands": {str(demand): {}}}), newest),
        )
        db.execute(
            "DELETE FROM score_breakdowns WHERE result_id IN "
            "(SELECT result_id FROM match_results WHERE run_id=?)",
            (newest,),
        )
        db.execute("DELETE FROM match_results WHERE run_id=?", (newest,))
        assert db.execute("SELECT count(*) FROM chat_v_match_results").fetchone()[0] == 0


def test_query_retries_exhausted(api: Api) -> None:
    client, _ = api
    stub = ChatStub()
    stub.lookup = {
        "intent": "query",
        "sql": "SELECT * FROM candidates",
        "explanation": "This lists records.",
    }
    client.app.state.llm_provider = stub
    result = _answer(api, "List the available resources.")["answer"]
    assert result["outcome"] == "uninterpretable"
    assert "top ranked" in result["text"]
    assert len(stub.requests) == 3


def _ask(api: Api, question: str) -> dict[str, Any]:
    client, csrf = api
    response = client.post("/api/v1/chat", json={"question": question}, headers=csrf)
    assert response.status_code == 202, response.text
    return response.json()


def _answer(api: Api, question: str) -> dict[str, Any]:
    client, _ = api
    job_id = _ask(api, question)["jobId"]
    assert wait_done(client.app.state.job_runner, job_id).status == "completed"
    result = client.get(f"/api/v1/chat/{job_id}")
    assert result.status_code == 200, result.text
    return result.json()


def _seed(api: Api) -> None:
    client, _ = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, "T10001", mandatory=("SQL",), preferred=("SQL", "Python"))
        seed_demand(db, "T10002", mandatory=("SQL", "Java"))
        seed_demand(db, "T10003", mandatory=("SQL", "Databricks"))
        seed_demand(db, "T10004", mandatory=("NeverCount",))
        seed_demand(db, "T10005", confirmed=False)
        db.execute("UPDATE demands SET bu='INS'")
        db.execute("UPDATE demands SET bu='OTHER' WHERE code_requisition='T10003'")
        db.execute("UPDATE demands SET lifecycle='archived' WHERE code_requisition='T10004'")
        db.execute(
            "UPDATE demands SET position_start_date='2026-12-01' WHERE code_requisition='T10005'"
        )
        db.execute("UPDATE demands SET account_name='Acme' WHERE demand_id=?", (demand,))
        candidate = seed_candidate(db)
        db.execute(
            "UPDATE candidates SET display_name='Bea Weiß' WHERE candidate_id=?", (candidate,)
        )
        first = seed_resource(
            db, "BeaS", grade="C1", skills="DBX", candidate=candidate, email="bea@example.test"
        )
        second_candidate = db.execute(
            "INSERT INTO candidates(internal_ref,identity_status,created_at,updated_at) "
            "VALUES ('PFC-C-CHAT','incomplete',?,?)",
            (STAMP, STAMP),
        ).lastrowid
        second = seed_resource(db, "CaseyS", grade="C2", skills="SQL", candidate=second_candidate)
        old = seed_cv(db, second, second_candidate, skills=("Databricks", "OldSkill"))
        db.execute("UPDATE cv_versions SET is_latest=0 WHERE cv_version_id=?", (old,))
        latest = db.execute(
            "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
            "extracted_by,is_latest,extracted_at) SELECT resume_id,candidate_id,2,'current',"
            "'Built pipelines with Databricks.','test',1,extracted_at "
            "FROM cv_versions WHERE cv_version_id=?",
            (old,),
        ).lastrowid
        db.execute(
            "INSERT INTO resource_skills(resource_id,cv_version_id,source,skill_raw,skill_norm) "
            "VALUES (?,?,'CV','PySpark','pyspark')",
            (second, latest),
        )
        outside = seed_resource(db, "OutsideRange", grade="B2", skills="Databricks")
        archived = seed_resource(db, "Archived", skills="Databricks")
        seed_resource(db, "Excluded", skills="Databricks")
        db.execute(
            "UPDATE resources SET eligibility='eligible' WHERE resource_id IN (?,?,?,?)",
            (first, second, outside, archived),
        )
        db.execute("UPDATE resources SET lifecycle='archived' WHERE resource_id=?", (archived,))
        skill = db.execute(
            "INSERT INTO skills(name,created_by) VALUES ('Databricks','admin') "
            "ON CONFLICT(name) DO UPDATE SET is_active=1 RETURNING skill_id"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,credit,status,updated_at)"
            " VALUES (?,'DBX','dbx','abbreviation',1,'approved',?)",
            (skill, STAMP),
        )
        model = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active)"
            " VALUES ('test-chat','onnx','hash',2,'v1',0)"
        ).lastrowid
        for score in (60, 84.5):
            run = db.execute(
                "INSERT INTO match_runs(scope,template_id,embedding_model_id,engine_version,"
                "synonyms_version,config_hash,status) SELECT 'demands',template_id,?,'test','v1',"
                "'hash','completed' FROM scoring_templates WHERE status='active'",
                (model,),
            ).lastrowid
            result = db.execute(
                "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                "resource_id,"
                "score,band,rank,shortlisted,grade_relation,location_assessment,matching_evidence,"
                "gaps_checks,eligibility_evidence) "
                "SELECT ?,d.demand_id,d.current_version_id,q.jd_req_id,?,?,'Potential',2,1,"
                "'exact','Aligned','sensitive evidence','[]','[]' FROM demands d "
                "JOIN jd_requirements q ON q.demand_version_id=d.current_version_id "
                "WHERE d.demand_id=?",
                (run, first, score, demand),
            ).lastrowid
            db.execute(
                "INSERT INTO score_breakdowns VALUES (?,'skills',58,70,?)",
                (result, '{"email":"bea@example.test"}'),
            )


def test_offline_reference_questions(api: Api) -> None:
    _seed(api)
    client, _ = api
    stub = ChatStub()
    client.app.state.llm_provider = stub
    assert client.get("/api/v1/chat/status").json() == {"available": True}
    references = [
        (
            "How many open demands are there?",
            {"intent": "demand_count"},
            "Count of open demands: 4.",
            ["T10001", "T10002", "T10003", "T10005"],
        ),
        (
            "How many open demands are there for account Acme?",
            {"intent": "demand_count", "scope_type": "account", "scope_term": "Acme"},
            "Count of open demands for account Acme: 1.",
            ["T10001"],
        ),
        (
            "Top 5 skills in demand for INS this month",
            {
                "intent": "demand_skills",
                "scope_type": "bu",
                "scope_term": "INS",
                "period": "this_month",
            },
            "Top confirmed skills by number of open demands for business unit INS starting in "
            "2026-11: SQL (2), Java (1), Python (1).",
            ["T10001", "T10002"],
        ),
        (
            "Who on the bench has Databricks and is C1–C2?",
            {"intent": "bench_skill", "skill": "Databricks", "grade_from": "C1", "grade_to": "C2"},
            "Eligible active bench resources with Databricks at grade C1 through C2: 2.",
            ["Resource #1", "Resource #2"],
        ),
        (
            "Who on the bench has PySpark?",
            {"intent": "bench_skill", "skill": "PySpark"},
            "Eligible active bench resources with Spark: 1.",
            ["Resource #2"],
        ),
        (
            "Why is Bea Weiß ranked #2 for T10001? bea@example.test 07700 900123",
            {"intent": "match_rank", "requisition": "T10001", "rank": 2, "resource_id": 1},
            "In completed run #2, Resource #1 was ranked #2 for T10001 with a score of 84.5/100 "
            "(Potential). The stored scoring components are shown in the source.",
            ["Match result #2"],
        ),
        (
            "Who on the bench has OldSkill?",
            {"intent": "bench_skill", "skill": "OldSkill"},
            "I couldn't find that in PFC data. No eligible active bench resources with OldSkill.",
            [],
        ),
    ]
    for question, lookup, text, sources in references:
        stub.lookup = lookup
        entry = _answer(api, question)
        answer = entry["answer"]
        assert answer["text"] == text
        assert [source["label"] for source in answer["sources"]] == sources
        if lookup.get("skill") == "Databricks":
            assert [source["values"]["Evidence"] for source in answer["sources"]] == [
                "CT",
                "CV v2 (#2, text)",
            ]
        assert answer["outcome"] == ("answered" if sources else "no_data")
        assert stub.requests[-1].purpose == "chat_lookup"
        assert stub.requests[-1].json_schema == Lookup.model_json_schema()
    assert "Bea Weiß" not in str(stub.requests)
    assert "bea@example.test" not in str(stub.requests)
    assert "07700" not in str(stub.requests)
    assert "sensitive evidence" not in str(stub.requests)
    assert "Bea Weiß" not in client.get("/api/v1/chat").text
    assert "bea@example.test" not in client.get("/api/v1/chat").text
    jobs = [job for job in client.get("/api/v1/jobs").json() if job["kind"] == "chat"]
    assert len(jobs) == len(references)
    assert all(
        job["label"] == "Chat question" and job["link"] == f"/#/chat?job={job['jobId']}"
        for job in jobs
    )
    with database(client.app.state.settings) as db:
        assert verify_chain(db)
        audit = db.execute(
            "SELECT action,details_json FROM audit_log WHERE entity_type=? ORDER BY audit_id",
            ("chat",),
        ).fetchall()
        assert [row["action"] for row in audit] == ["CHAT_QUESTION", "CHAT_ANSWER"] * len(
            references
        )
        assert json.loads(audit[-3]["details_json"])["sources"] == [["match_result", 2]]
        assert "bea@example.test" not in str([tuple(row) for row in audit])
        assert db.execute("SELECT score FROM match_results WHERE run_id=2").fetchone()[0] == 84.5


@pytest.mark.parametrize(
    ("mode", "lookup", "outcome"),
    [
        ("local", {"intent": "unsupported"}, "unsupported"),
        ("local", {"intent": "demand_count", "sql": "SELECT * FROM resources"}, "uninterpretable"),
        ("local", {"intent": "bench_skill", "skill": "Guess"}, "uninterpretable"),
        ("external", {}, "unavailable"),
        ("down", {}, "unavailable"),
    ],
)
def test_chat_fails_closed(api: Api, mode: str, lookup: dict[str, Any], outcome: str) -> None:
    client, _ = api
    stub = ChatStub(mode=mode)
    stub.lookup = lookup
    client.app.state.llm_provider = stub
    response = _answer(api, "Who should I staff for this demand?")
    assert response["answer"]["outcome"] == outcome
    assert response["answer"]["sources"] == []
    if mode in {"external", "down"}:
        assert client.get("/api/v1/chat/status").json() == {"available": False}
        assert not stub.requests


@pytest.mark.parametrize(
    ("stored", "typed"),
    [
        ("Işık Yılmaz", "ışık yılmaz"),
        ("Işık Yılmaz", "IŞIK YILMAZ"),
        ("Işık Yılmaz", "İşık Yılmaz"),
        ("İpek Yılmaz", "i\u0307pek yılmaz"),
        ("i\u0307pek Yılmaz", "İPEK YILMAZ"),
    ],
)
def test_chat_masks_turkish_name_variants(api: Api, stored: str, typed: str) -> None:
    client, _ = api
    with database(client.app.state.settings) as db:
        resource_id = seed_resource(db, stored)
    stub = ChatStub()
    stub.lookup = {"intent": "unsupported"}
    client.app.state.llm_provider = stub

    result = _answer(api, f"What is {typed}'s email?")
    masked = f"What is Resource #{resource_id}'s email?"
    assert stub.requests[0].messages[-1]["content"] == masked
    assert result["question"] == masked
    assert client.get("/api/v1/chat").json()[0]["question"] == masked


def test_chat_is_private_and_can_be_stopped(api: Api) -> None:
    client, csrf = api
    stub = ChatStub(mode="slow")
    client.app.state.llm_provider = stub
    assert client.post("/api/v1/chat", json={"question": "   "}, headers=csrf).status_code == 422
    entry = _ask(api, "How many open demands are there?")
    assert stub.entered.wait(5)
    job_id = entry["jobId"]
    path = f"/api/v1/jobs/{job_id}"
    assert client.post(path + "/cancel", headers=csrf).status_code == 200
    assert wait_done(client.app.state.job_runner, job_id).status == "cancelled"
    detail = f"/api/v1/chat/{job_id}"
    assert client.get(detail).json()["answer"] is None
    settings = client.app.state.settings
    with database(settings) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action='CHAT_CANCEL_REQUESTED' AND entity_id=?",
                (str(job_id),),
            ).fetchone()[0]
            == 1
        )
        role = db.execute("SELECT role_id FROM roles WHERE name='Analyst'").fetchone()[0]
        create_user(
            db,
            email="someone.else@example.test",
            display_name="Second Analyst",
            role_id=role,
            password_hash=hasher(settings).hash(PASSWORD),
            now=NOW.isoformat(),
            must_change_password=False,
        )
    client.cookies.clear()
    assert client.get(detail).status_code == 401
    assert client.get("/api/v1/chat").status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"role": "Analyst", "email": "someone.else@example.test", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert client.get("/api/v1/chat").json() == []
    assert client.get("/api/v1/jobs").json() == []
    assert client.get(detail).status_code == 404
    assert client.get(path).status_code == 404
    assert client.get(path + "/events").status_code == 404
    assert (
        client.post(
            path + "/cancel", headers={"X-CSRF-Token": login.json()["csrf_token"]}
        ).status_code
        == 404
    )
