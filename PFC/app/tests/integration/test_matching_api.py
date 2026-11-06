"""Authenticated matching API contracts (REQ-AUTH-01/04, REQ-MAT-02/09, REQ-OUT-03)."""

import json

import pytest
from fastapi.testclient import TestClient
from test_matching_service import (
    NOW,
    FakeEmbedder,
    seed_demand,
    seed_resource,
    wait_done,
)

from pfc.api.routers.match import router as match_router
from pfc.api.routers.results import router as results_router
from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.matching import MatchingPorts

PASSWORD = "Strong!Random4567"
SAMPLE_EMAIL = "AlexBorder@capgemini.com"


@pytest.fixture(params=["Analyst", "Admin"])
def api(request, tmp_path):
    role = request.param
    settings = Settings(
        data_dir=tmp_path,
        frontend_dir=tmp_path / "no-frontend",
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
        job_workers=1,
    )
    app = create_app(settings)
    app.include_router(match_router, prefix="/api/v1")
    app.include_router(results_router, prefix="/api/v1")
    app.state.clock = lambda: NOW
    app.state.matching_ports = MatchingPorts(FakeEmbedder())
    with TestClient(app, raise_server_exceptions=False) as client:
        setup = client.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": SAMPLE_EMAIL,
                "display_name": "Reviewer",
                "password": PASSWORD,
            },
        )
        assert setup.status_code == 201, setup.text
        with database(settings) as db:
            db.executemany(
                "INSERT INTO grades(code,ordinal) VALUES (?,?)",
                [(code, i) for i, code in enumerate(load_domain_config().grades.codes, 1)],
            )
            # Reuse the allowed sample identity; the Analyst case is an actual Analyst account.
            if role == "Analyst":
                db.execute(
                    "UPDATE users SET role_id=(SELECT role_id FROM roles WHERE name='Analyst')"
                )
        logged_in = client.post(
            "/api/v1/auth/login",
            json={
                "role": role,
                "email": SAMPLE_EMAIL,
                "password": PASSWORD,
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()["user"]["role"] == role
        yield client, {"X-CSRF-Token": logged_in.json()["csrf_token"]}


def problem(response, status, code=None):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    if code:
        assert response.json()["code"] == code


def test_req_auth_01_matching_requires_login(api):
    client, _ = api
    client.cookies.clear()
    for method, path, body in [
        ("POST", "/match/analyze-jd/1", None),
        ("GET", "/match/requirements/1", None),
        ("POST", "/match/runs", {"scope": "full"}),
        ("GET", "/match/runs", None),
        ("GET", "/match/runs/1", None),
        ("GET", "/results", None),
        ("GET", "/results/excluded", None),
        ("GET", "/results/1", None),
        ("POST", "/results/1/overrides", {"action": "pin", "reason": "Review"}),
        ("DELETE", "/results/overrides/1", {"reason": "Review"}),
        ("GET", "/demands/1/matches", None),
    ]:
        response = client.request(method, "/api/v1" + path, json=body)
        problem(response, 401, "AUTH_REQUIRED")


def test_req_auth_04_matching_writes_require_csrf(api):
    client, _ = api
    for method, path, body in [
        ("POST", "/match/analyze-jd/1", None),
        ("POST", "/match/runs", {"scope": "full"}),
        ("POST", "/results/1/overrides", {"action": "pin", "reason": "Review"}),
        ("DELETE", "/results/overrides/1", {"reason": "Review"}),
    ]:
        problem(client.request(method, "/api/v1" + path, json=body), 403, "CSRF_INVALID")


def test_req_mat_02_09_full_api_flow_and_weak_only(api):
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        weak_id = seed_demand(db, "T1WEAK", confirmed=False)
        seed_resource(db, email=SAMPLE_EMAIL)
        seed_resource(db, "Excluded", status="Selected", group="Not on Bench")
        seed_resource(db, "GradeGap", grade="E1")
    analyzed = client.post(f"/api/v1/match/analyze-jd/{demand_id}", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["status"] == "draft" and analyzed.json()["extracted_by"] == "rule_based"
    get_req = client.get(f"/api/v1/match/requirements/{demand_id}")
    assert (
        get_req.status_code == 200 and get_req.json()["jd_req_id"] == analyzed.json()["jd_req_id"]
    )
    response = client.post(
        "/api/v1/match/runs",
        headers=headers,
        json={
            "scope": "demands",
            "demand_ids": [demand_id, weak_id],
            "edited_requirements": {
                str(demand_id): None,
                str(weak_id): {
                    "mandatory_skills": [
                        {"text": term}
                        for term in ("SQL", "Rust", "Go", "Scala", "R", "Cobol", "Kotlin")
                    ]
                },
            },
            "as_of_date": "2026-11-06",
        },
    )
    assert response.status_code == 202, response.text
    run_id, job_id = response.json()["run_id"], response.json()["job_id"]
    assert wait_done(client.app.state.job_runner, job_id).status == "completed"
    run = client.get(f"/api/v1/match/runs/{run_id}")
    assert run.status_code == 200 and run.json()["status"] == "completed"
    assert json.loads(run.json()["scope_json"])["job_id"] == job_id
    assert client.get("/api/v1/match/runs").json()[0]["run_id"] == run_id
    assert client.get(f"/api/v1/match/requirements/{demand_id}").json()["status"] == "confirmed"
    rows = client.get("/api/v1/results", params={"run_id": run_id, "requisition": "T1TEST"})
    assert rows.status_code == 200, rows.text
    assert rows.json()["total"] == 1
    row = rows.json()["items"][0]
    assert row["person_email"] == "Al******er@capgemini.com"
    assert row["band"] == "Potential"
    assert SAMPLE_EMAIL not in rows.text
    detail = client.get(f"/api/v1/results/{row['result_id']}")
    assert detail.status_code == 200 and len(detail.json()["breakdown"]) == 6
    assert SAMPLE_EMAIL not in detail.text
    normal = client.get(f"/api/v1/demands/{demand_id}/matches")
    assert normal.status_code == 200 and not normal.json()["no_suitable_candidate"]
    weak = client.get(f"/api/v1/demands/{weak_id}/matches")
    assert weak.status_code == 200 and weak.json()["no_suitable_candidate"]
    assert weak.json()["weak_only"] and weak.json()["items"][0]["display_band"] == "Weak Match"
    assert weak.json()["items"][0]["shortlisted"]
    endpoint = f"/api/v1/results/{row['result_id']}/overrides"
    for body in [
        {"action": "pin", "reason": " \n"},
        {"action": "pin"},
        {"action": "adjust_band", "reason": "Review"},
        {"action": "adjust_band", "reason": "Review", "new_band": "Invalid"},
    ]:
        problem(client.post(endpoint, headers=headers, json=body), 422, "VALIDATION_ERROR")
    override = client.post(
        endpoint,
        headers=headers,
        json={
            "action": "adjust_band",
            "reason": "Reviewed evidence",
            "new_band": "Strong",
        },
    )
    assert override.status_code == 201, override.text
    override_id = override.json()["override_id"]
    changed = client.get("/api/v1/results", params={"band": "Strong"}).json()["items"][0]
    assert changed["effective_band"] == "Strong" and changed["score"] == row["score"]
    assert changed["overridden"]
    revoke_url = f"/api/v1/results/overrides/{override_id}"
    problem(client.request("DELETE", revoke_url, headers=headers, json={"reason": " "}), 422)
    revoked = client.request(
        "DELETE", revoke_url, headers=headers, json={"reason": "Revert review"}
    )
    assert revoked.status_code == 204
    problem(client.request("DELETE", revoke_url, headers=headers, json={"reason": "Again"}), 409)
    exclusions = client.get("/api/v1/results/excluded")
    assert exclusions.status_code == 200, exclusions.text
    reasons = {(item["short_name"], item["reason_code"]) for item in exclusions.json()}
    assert reasons == {
        ("Excluded", "STATUS_EXCLUDED"),
        ("Excluded", "NOT_ON_BENCH"),
        ("GradeGap", "GRADE_GAP_GT1"),
    }
    with database(settings) as db:
        assert verify_chain(db)
        actions = [row[0] for row in db.execute("SELECT action FROM audit_log ORDER BY audit_id")]
        assert "match.override" in actions and "match.override.revoke" in actions
        assert (
            db.execute(
                "SELECT created_by FROM overrides WHERE override_id=?",
                (override_id,),
            ).fetchone()[0]
            == 1
        )


def test_req_mat_02_ports_absent_and_conflicts(api):
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        blank_id = seed_demand(db, "T1BLANK", confirmed=False, jd="")
    del client.app.state.matching_ports
    draft = client.post(f"/api/v1/match/analyze-jd/{demand_id}", headers=headers)
    assert draft.status_code == 200 and draft.json()["extracted_by"] == "rule_based"
    problem(
        client.post(
            "/api/v1/match/runs",
            headers=headers,
            json={
                "edited_requirements": {str(demand_id): None},
            },
        ),
        503,
        "MATCHING_UNAVAILABLE",
    )
    assert client.get(f"/api/v1/match/requirements/{demand_id}").json()["status"] == "draft"
    client.app.state.matching_ports = MatchingPorts(None)
    problem(client.post("/api/v1/match/runs", headers=headers, json={}), 503)
    client.app.state.matching_ports = MatchingPorts(FakeEmbedder())
    problem(client.post(f"/api/v1/match/analyze-jd/{blank_id}", headers=headers), 409)
    problem(
        client.post(
            "/api/v1/match/runs",
            headers=headers,
            json={
                "edited_requirements": {str(blank_id): None},
            },
        ),
        409,
    )
    for body in [
        {"scope": "demands"},
        {"scope": "unknown"},
        {"scope": "full", "demand_ids": [demand_id]},
        {"scope": "demands", "demand_ids": [demand_id, demand_id]},
        {
            "scope": "demands",
            "demand_ids": [demand_id],
            "edited_requirements": {str(blank_id): None},
        },
        {"edited_requirements": {str(demand_id): {"mandatory_skills": [{"text": " "}]}}},
    ]:
        problem(client.post("/api/v1/match/runs", headers=headers, json=body), 422)


def test_req_out_01_empty_missing_validation_and_openapi(api):
    client, headers = api
    assert client.get("/api/v1/results").json()["items"] == []
    assert client.get("/api/v1/results/excluded").json() == []
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, confirmed=False)
    problem(client.get(f"/api/v1/match/requirements/{demand_id}"), 404)
    for path in [
        "/match/requirements/999",
        "/match/runs/999",
        "/results/999",
        "/results?run_id=999",
        "/results/excluded?run_id=999",
        "/demands/999/matches",
    ]:
        problem(client.get("/api/v1" + path), 404)
    problem(client.post("/api/v1/match/analyze-jd/999", headers=headers), 404)
    problem(
        client.post(
            "/api/v1/match/runs", headers=headers, json={"scope": "demands", "demand_ids": [999]}
        ),
        404,
    )
    problem(
        client.post(
            "/api/v1/results/999/overrides",
            headers=headers,
            json={"action": "pin", "reason": "Review"},
        ),
        404,
    )
    problem(
        client.request(
            "DELETE", "/api/v1/results/overrides/999", headers=headers, json={"reason": "Review"}
        ),
        404,
    )
    for query in [
        "limit=0",
        "offset=-1",
        "sort=invalid",
        "score_min=90&score_max=10",
        "band=invalid",
        "run_id=-1",
    ]:
        problem(client.get("/api/v1/results?" + query), 422)
    empty = client.get(f"/api/v1/demands/{demand_id}/matches")
    assert empty.status_code == 200 and empty.json()["items"] == []
    schema = client.get("/openapi.json")
    assert schema.status_code == 200, schema.text
    assert "/api/v1/results/excluded" in schema.json()["paths"]
    assert "/api/v1/match/runs" in schema.json()["paths"]
