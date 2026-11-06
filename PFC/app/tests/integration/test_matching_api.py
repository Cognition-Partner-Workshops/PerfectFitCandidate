"""TDD §12 authenticated contracts against the production app and real SQLite."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from test_matching_service import (
    NOW,
    FakeEmbedder,
    FakeLlm,
    seed_candidate,
    seed_demand,
    seed_resource,
    wait_done,
)

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.matching import MatchingPorts, confirm_requirements

PASSWORD = "Strong!Random4567"
SAMPLE_EMAIL = "AlexBorder@capgemini.com"
Api = tuple[TestClient, dict[str, str]]


@pytest.fixture(params=["Analyst", "Admin"])
def api(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Api]:
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
            if request.param == "Analyst":
                db.execute(
                    "UPDATE users SET role_id=(SELECT role_id FROM roles WHERE name='Analyst')"
                )
        login = client.post(
            "/api/v1/auth/login",
            json={
                "role": request.param,
                "email": SAMPLE_EMAIL,
                "password": PASSWORD,
            },
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["role"] == request.param
        yield client, {"X-CSRF-Token": login.json()["csrf_token"]}


def problem(response: Response, status: int, code: str | None = None) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    if code:
        assert response.json()["code"] == code


def writes() -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("POST", "/demands/1/analyze", None),
        ("POST", "/demands/analyze", {"demandIds": [1]}),
        ("PUT", "/demands/1/requirements", {"demandId": 1, "mandatorySkills": ["SQL"]}),
        ("POST", "/demands/1/requirements/confirm", None),
        ("POST", "/match-runs", {"scope": "full"}),
        ("POST", "/match-results/1/overrides", {"action": "pin", "reason": "Review"}),
        ("DELETE", "/match-results/1/overrides", {"reason": "Review"}),
    ]


def test_req_auth_01_all_routes_require_login(api: Api) -> None:
    client, _ = api
    client.cookies.clear()
    calls = writes() + [
        ("GET", path, None)
        for path in (
            "/demands/1/requirements",
            "/match-runs",
            "/match-runs/1",
            "/match-runs/1/results",
            "/match-results/1",
            "/resources/excluded",
        )
    ]
    for method, path, body in calls:
        problem(client.request(method, "/api/v1" + path, json=body), 401, "AUTH_REQUIRED")


def test_req_auth_04_each_write_requires_csrf(api: Api) -> None:
    client, _ = api
    for method, path, body in writes():
        problem(client.request(method, "/api/v1" + path, json=body), 403, "CSRF_INVALID")


def finish(client: TestClient, response: Response) -> int:
    assert response.status_code == 202, response.text
    assert set(response.json()) == {"runId", "jobId"}
    assert wait_done(client.app.state.job_runner, response.json()["jobId"]).status == "completed"
    return response.json()["runId"]


def test_req_mat_02_full_flow_drafts_results_overrides_exclusions(api: Api) -> None:
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        seed_demand(db, "T1WEAK", mandatory=("Rust", "Go", "Scala", "Cobol", "Kotlin"))
        candidate = seed_candidate(db)
        db.execute(
            "UPDATE candidates SET classification='External',classification_override='Internal' "
            "WHERE candidate_id=?",
            (candidate,),
        )
        seed_resource(db, email=SAMPLE_EMAIL, candidate=candidate)
        seed_resource(db, "Excluded", status="Selected", group="Not on Bench", email=SAMPLE_EMAIL)
        seed_resource(db, "GradeGap", grade="E1")
        db.execute(
            "UPDATE demands SET bu='DATA',region='Manchester' WHERE demand_id=?", (demand_id,)
        )
    analyzed = client.post(f"/api/v1/demands/{demand_id}/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    draft = analyzed.json()
    assert draft["demandId"] == demand_id and draft["status"] == "draft"
    assert draft["mandatorySkills"] == ["SQL"] and draft["preferredSkills"] == ["Python"]
    assert draft["experienceYears"] is None and draft["qualifications"] == ""
    assert draft["domain"] == "" and draft["location"] == "" and not draft["mandatoryOnsite"]
    assert draft["synonymsApplied"] == [] and draft["warnings"] == []
    assert client.get(f"/api/v1/demands/{demand_id}/requirements").json() == draft
    # An existing confirmed revision survives any number of edits to the latest draft.
    with database(settings) as db:
        confirmed = confirm_requirements(db, demand_id, None, 1, NOW)["jd_req_id"]
    draft.update(
        experienceYears=5,
        qualifications="Degree; Certification",
        domain="Finance; Retail",
        location="London",
        mandatoryOnsite=True,
    )
    edited = client.put(f"/api/v1/demands/{demand_id}/requirements", headers=headers, json=draft)
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "draft" and edited.json()["extractedBy"] == "manual"
    assert edited.json()["experienceYears"] == 5
    assert edited.json()["qualifications"] == "Degree; Certification"
    assert edited.json()["domain"] == "Finance; Retail"
    revised = client.put(
        f"/api/v1/demands/{demand_id}/requirements", headers=headers, json=edited.json()
    )
    assert revised.status_code == 200
    assert revised.json()["revision"] == edited.json()["revision"] + 1
    with database(settings) as db:
        assert (
            db.execute(
                "SELECT status FROM jd_requirements WHERE jd_req_id=?", (confirmed,)
            ).fetchone()[0]
            == "confirmed"
        )
        assert (
            db.execute("SELECT COUNT(*) FROM jd_requirements WHERE status='draft'").fetchone()[0]
            == 1
        )
    run_id = finish(
        client,
        client.post(
            f"/api/v1/demands/{demand_id}/requirements/confirm",
            headers=headers,
        ),
    )
    assert client.get(f"/api/v1/demands/{demand_id}/requirements").json()["status"] == "confirmed"
    problem(client.post(f"/api/v1/demands/{demand_id}/requirements/confirm", headers=headers), 409)
    run = client.get(f"/api/v1/match-runs/{run_id}")
    assert run.status_code == 200 and run.json()["status"] == "completed"
    assert isinstance(run.json()["llmModel"], str) and run.json()["templateVersion"] == 1
    assert client.get("/api/v1/match-runs?limit=1&offset=0").json() == [run.json()]
    with database(settings) as db:
        db.execute("UPDATE resources SET grade='D2',location='Glasgow' WHERE short_name='Person'")
        db.execute(
            "UPDATE demands SET bu='Changed',region='Glasgow' WHERE demand_id=?", (demand_id,)
        )
    rows = client.get(f"/api/v1/match-runs/{run_id}/results")
    assert rows.status_code == 200, rows.text
    assert rows.headers["X-Total-Count"] == "1"
    row = rows.json()[0]
    assert row["bu"] == "DATA" and row["region"] == "Manchester"
    assert row["personGrade"] == "C1" and row["personLocation"] == "Manchester"
    assert row["positionStartDate"] == "2026-11-16" and row["demandRequestName"] == "Data role"
    assert row["personEmailMasked"] == "Al******er@capgemini.com" and SAMPLE_EMAIL not in rows.text
    assert row["candidateType"] == "Internal" and row["incompleteIdentity"]
    assert "override" not in row and len(row["breakdown"]) == 6
    for key in ("matchingEvidence", "gapsChecks", "eligibilityEvidence"):
        assert isinstance(row[key], list) and all(isinstance(value, str) for value in row[key])
    detail = client.get(f"/api/v1/match-results/{row['resultId']}")
    assert detail.status_code == 200 and detail.json()["breakdown"][0]["inputs"]
    assert "cvScores" in detail.json() and detail.json()["overrides"] == []
    endpoint = f"/api/v1/match-results/{row['resultId']}/overrides"
    for body in [
        {"action": "pin", "reason": " \n"},
        {"action": "pin"},
        {"action": "adjust_band", "reason": "Review"},
        {"action": "adjust_band", "reason": "Review", "newBand": "Invalid"},
    ]:
        problem(client.post(endpoint, headers=headers, json=body), 422)
    for label in ("Avg.", "Avg"):
        changed = client.post(
            endpoint,
            headers=headers,
            json={
                "action": "adjust_band",
                "reason": "Reviewed",
                "newBand": label,
            },
        )
        assert changed.status_code == 201, changed.text
        assert (
            changed.json()["effectiveBand"] == "Avg"
            and changed.json()["override"]["newBand"] == "Avg"
        )
        assert changed.json()["override"]["createdBy"] == "Reviewer"
        assert changed.json()["score"] == row["score"]
    for label in ("Avg.", "Avg"):
        filtered = client.get(
            f"/api/v1/match-runs/{run_id}/results",
            params={
                "band": label,
                "personGrade": "C1",
                "sort": "personGrade",
                "limit": 5000,
            },
        )
        assert filtered.status_code == 200 and len(filtered.json()) == 1
    history = client.get(f"/api/v1/match-results/{row['resultId']}").json()["overrides"]
    first, last = history[0]["overrideId"], history[-1]["overrideId"]
    for params, body in [({}, None), ({"reason": " "}, None), ({}, {"reason": " "})]:
        problem(client.request("DELETE", endpoint, headers=headers, params=params, json=body), 422)
    revoked = client.request(
        "DELETE",
        endpoint,
        headers=headers,
        params={
            "overrideId": first,
            "reason": "Undo first",
        },
    )
    assert revoked.status_code == 200 and revoked.json()["override"]["overrideId"] == last
    problem(
        client.request(
            "DELETE",
            endpoint,
            headers=headers,
            params={
                "overrideId": first,
                "reason": "Again",
            },
        ),
        404,
    )
    revoked = client.request("DELETE", endpoint, headers=headers, json={"reason": "Undo latest"})
    assert revoked.status_code == 200 and "override" not in revoked.json()
    for action in ("pin", "unpin", "exclude", "include"):
        changed = client.post(
            endpoint, headers=headers, json={"action": action, "reason": "Review"}
        )
        assert changed.status_code == 201 and changed.json()["override"]["action"] == action
        assert "newBand" not in changed.json()["override"]
    active_override = changed.json()["override"]["overrideId"]
    excluded = client.get("/api/v1/resources/excluded")
    assert excluded.status_code == 200, excluded.text
    assert len(excluded.json()) == 1 and excluded.json()[0]["shortName"] == "Excluded"
    assert (
        excluded.json()[0]["eligibility"] == "excluded"
        and len(excluded.json()[0]["exclusionReasons"]) == 2
    )
    assert excluded.json()[0]["emailAddressMasked"] == "Al******er@capgemini.com"
    assert SAMPLE_EMAIL not in excluded.text
    assert (
        client.get("/api/v1/resources/excluded", params={"runId": run_id}).json() == excluded.json()
    )
    with database(settings) as db:
        db.execute("UPDATE resources SET grade='C1' WHERE short_name='Person'")
    full_id = finish(
        client,
        client.post(
            "/api/v1/match-runs",
            headers=headers,
            json={
                "scope": "full",
                "asOfDate": "2026-11-06",
            },
        ),
    )
    weak = client.get(f"/api/v1/match-runs/{full_id}/results", params={"requisition": "T1WEAK"})
    assert weak.status_code == 200 and weak.json()[0]["band"] == "Weak"
    problem(
        client.request(
            "DELETE",
            f"/api/v1/match-results/{weak.json()[0]['resultId']}/overrides",
            headers=headers,
            params={"overrideId": active_override, "reason": "Wrong result"},
        ),
        404,
    )
    with database(settings) as db:
        assert verify_chain(db)
        actions = {value[0] for value in db.execute("SELECT action FROM audit_log")}
        assert {"jd.edit", "jd.confirm", "match.override", "match.override.revoke"} <= actions


def test_req_mat_02_bulk_llm_and_unavailable_are_atomic(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        blank_id = seed_demand(db, "T1BLANK", confirmed=False, jd="")
    client.app.state.matching_ports = MatchingPorts(FakeEmbedder(), FakeLlm())
    bulk = client.post(
        "/api/v1/demands/analyze", headers=headers, json={"demandIds": [999, demand_id, blank_id]}
    )
    assert bulk.status_code == 200, bulk.text
    assert [item["demandId"] for item in bulk.json()["analyzed"]] == [demand_id]
    assert {item["demandId"] for item in bulk.json()["failed"]} == {999, blank_id}
    result = bulk.json()["analyzed"][0]
    assert result["experienceYears"] == 5 and result["qualifications"] == "degree"
    assert result["domain"] == "insurance" and result["location"] == "Manchester"
    assert result["synonymsApplied"] == ["BA → Business Analysis"]
    for ports in (None, MatchingPorts()):
        client.app.state.matching_ports = ports
        for path, body in [
            (f"/demands/{demand_id}/requirements/confirm", None),
            ("/match-runs", {"scope": "full"}),
        ]:
            problem(client.post("/api/v1" + path, headers=headers, json=body), 503)
        assert client.get(f"/api/v1/demands/{demand_id}/requirements").json() == result
        with database(client.app.state.settings) as db:
            assert db.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0] == 0
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM jd_requirements WHERE status='confirmed'"
                ).fetchone()[0]
                == 0
            )
    # Analysis still works with no configured ports.
    fallback = client.post(f"/api/v1/demands/{demand_id}/analyze", headers=headers)
    assert fallback.status_code == 200 and fallback.json()["extractedBy"] == "rule_based"


def test_results_default_page_and_candidate_defaults(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_demand(db)
        for index in range(1005):
            seed_resource(db, f"Person{index:03d}")
    run_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    url = f"/api/v1/match-runs/{run_id}/results"
    rows = client.get(url)
    assert rows.status_code == 200 and len(rows.json()) == 1005
    assert rows.headers["X-Total-Count"] == "1005"
    assert rows.json()[0]["candidateId"] == 0
    assert rows.json()[0]["candidateType"] == "Unknown"
    assert not rows.json()[0]["incompleteIdentity"]
    page = client.get(url, params={"limit": 5, "offset": 1000})
    assert page.status_code == 200 and page.json() == rows.json()[1000:]
    assert page.headers["X-Total-Count"] == "1005"
    assert client.get(url, params={"offset": 1000}).json() == rows.json()[1000:]
    assert client.get(url, params={"offset": 2000}).json() == []
    assert client.get(url, params={"band": "Strong"}).json() == []
    parameters = client.get("/openapi.json").json()["paths"]["/api/v1/match-runs/{run_id}/results"][
        "get"
    ]["parameters"]
    limit = next(value for value in parameters if value["name"] == "limit")["schema"]
    assert limit.get("default") is None
    assert any(option.get("maximum") == 5000 for option in limit["anyOf"])


def test_experience_survives_flat_requirement_edits(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        confirm_requirements(
            db,
            demand_id,
            {
                "experience": [
                    {"text": "Experience managing teams"},
                    {"text": "Industry experience", "years": 3},
                ],
            },
            1,
            NOW,
        )
    url = f"/api/v1/demands/{demand_id}/requirements"
    original = client.get(url).json()
    assert original["experience"] == ["Experience managing teams", "Industry experience"]
    # A legacy client does not send the extra field or numeric years.
    legacy = {key: value for key, value in original.items() if key != "experience"}
    legacy["experienceYears"] = None
    saved = client.put(url, headers=headers, json=legacy)
    assert saved.status_code == 200 and saved.json()["experience"] == original["experience"]
    assert saved.json()["experienceYears"] == 3
    # The original revision is still the source even after another edit.
    replaced = client.put(
        url,
        headers=headers,
        json={
            "demandId": demand_id,
            "experience": ["Other experience"],
        },
    )
    assert replaced.status_code == 200
    legacy["experienceYears"] = 7
    saved = client.put(url, headers=headers, json=legacy)
    assert saved.status_code == 200 and saved.json()["experience"] == original["experience"]
    assert saved.json()["experienceYears"] == 7
    round_trip = client.put(url, headers=headers, json=saved.json())
    assert round_trip.status_code == 200
    assert round_trip.json()["experience"] == original["experience"]
    explicit = client.put(
        url,
        headers=headers,
        json={
            "demandId": demand_id,
            "experience": ["Leadership experience"],
            "experienceYears": 5,
        },
    )
    assert explicit.status_code == 200 and explicit.json()["experience"] == [
        "Leadership experience"
    ]
    assert explicit.json()["experienceYears"] == 5
    cleared = client.put(url, headers=headers, json={"demandId": demand_id, "experience": []})
    assert cleared.status_code == 200 and cleared.json()["experience"] == []
    problem(
        client.put(
            url,
            headers=headers,
            json={
                "demandId": demand_id,
                "experience": [" "],
            },
        ),
        409,
    )
    problem(
        client.put(
            url,
            headers=headers,
            json={
                "demandId": demand_id,
                "jdReqId": 999,
            },
        ),
        409,
    )


def test_empty_missing_validation_production_openapi_and_removed_paths(api: Api) -> None:
    client, headers = api
    assert client.get("/api/v1/match-runs").json() == []
    assert client.get("/api/v1/resources/excluded").json() == []
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, confirmed=False)
    problem(client.get(f"/api/v1/demands/{demand_id}/requirements"), 404)
    empty_draft = client.put(
        f"/api/v1/demands/{demand_id}/requirements",
        headers=headers,
        json={"demandId": demand_id, "mandatorySkills": [], "preferredSkills": []},
    )
    assert empty_draft.status_code == 200 and empty_draft.json()["status"] == "draft"
    assert empty_draft.json()["mandatorySkills"] == [] and empty_draft.json()["domain"] == ""
    for path in (
        "/demands/999/requirements",
        "/match-runs/999",
        "/match-runs/999/results",
        "/match-results/999",
        "/resources/excluded?runId=999",
    ):
        problem(client.get("/api/v1" + path), 404)
    problem(client.post("/api/v1/demands/999/analyze", headers=headers), 404)
    problem(client.post("/api/v1/demands/999/requirements/confirm", headers=headers), 404)
    problem(
        client.put("/api/v1/demands/999/requirements", headers=headers, json={"demandId": 999}), 404
    )
    problem(
        client.post(
            "/api/v1/match-runs", headers=headers, json={"scope": "demands", "demandIds": [999]}
        ),
        404,
    )
    for method in ("POST", "DELETE"):
        body = {"reason": "Review", **({"action": "pin"} if method == "POST" else {})}
        problem(
            client.request(
                method, "/api/v1/match-results/999/overrides", headers=headers, json=body
            ),
            404,
        )
    for body in [
        {"scope": "demands"},
        {"scope": "unknown"},
        {"demandIds": [demand_id]},
        {"scope": "demands", "demandIds": [demand_id, demand_id]},
        {"editedRequirements": {}},
    ]:
        problem(client.post("/api/v1/match-runs", headers=headers, json=body), 422)
    problem(
        client.put(
            f"/api/v1/demands/{demand_id}/requirements", headers=headers, json={"demandId": 999}
        ),
        422,
    )
    problem(
        client.put(
            f"/api/v1/demands/{demand_id}/requirements",
            headers=headers,
            json={"demandId": demand_id, "mandatorySkills": [" "]},
        ),
        422,
    )
    problem(client.post("/api/v1/demands/analyze", headers=headers, json={"demandIds": []}), 422)
    run_id = finish(
        client,
        client.post(
            "/api/v1/match-runs",
            headers=headers,
            json={
                "scope": "demands",
                "demandIds": [demand_id],
            },
        ),
    )
    empty = client.get(f"/api/v1/match-runs/{run_id}/results")
    assert empty.status_code == 200 and empty.json() == [] and empty.headers["X-Total-Count"] == "0"
    for query in (
        "limit=0",
        "limit=5001",
        "offset=-1",
        "sort=invalid",
        "scoreMin=90&scoreMax=10",
        "band=invalid",
    ):
        problem(client.get(f"/api/v1/match-runs/{run_id}/results?{query}"), 422)
    schema = client.get("/openapi.json")
    assert schema.status_code == 200, schema.text
    paths = schema.json()["paths"]
    for _, path, _ in writes():
        assert path.replace("/1/", "/{demand_id}/") in {
            item.removeprefix("/api/v1") for item in paths
        } or path.startswith("/match-results/")
    assert "/api/v1/match-runs/{run_id}/results" in paths
    assert "/api/v1/resources/excluded" in paths
    assert "/api/v1/match-results/{result_id}/overrides" in paths
    assert "/api/v1/demands/{demand_id}/matches" not in paths
    for path in ("/match/runs", "/results", "/results/excluded", f"/demands/{demand_id}/matches"):
        problem(client.get("/api/v1" + path), 404)
