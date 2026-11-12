"""TDD §12 authenticated contracts against the production app and real SQLite."""

import hashlib
import json
import shutil
from collections.abc import Iterable, Iterator
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
    seed_cv,
    seed_demand,
    seed_resource,
    wait_done,
)

from pfc.api.routers.resources import router as resources_router
from pfc.api.routers.results import router as results_router
from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.embeddings import runtime
from pfc.services.embeddings.chunking import OwnerType
from pfc.services.matching import MatchingPorts, confirm_requirements
from tests.unit.services.embeddings.conftest import tiny_bundle

PASSWORD = "Strong!Random4567"
SAMPLE_EMAIL = "AlexBorder@capgemini.com"
Api = tuple[TestClient, dict[str, str]]


@pytest.fixture(params=["Analyst", "Admin"])
def api(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Api]:
    settings = Settings(
        data_dir=tmp_path,
        frontend_dir=tmp_path / "no-frontend",
        bundled_models_dir=tmp_path / "bundled-models",
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


def test_confirm_starts_matching_with_registered_bundled_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        bundled_models_dir=tmp_path / "models",
        frontend_dir=tmp_path / "no-frontend",
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
        job_workers=1,
        llm_enabled=False,
    )
    source_model = tiny_bundle(tmp_path / "tiny-bundle")
    model_dir = settings.bundled_models_dir / "nomic-embed-text-v1.5"
    model_dir.mkdir(parents=True)
    model_path = model_dir / "model_int8.onnx"
    shutil.copyfile(source_model, model_path)
    shutil.copyfile(source_model.parent / "tokenizer.json", model_dir / "tokenizer.json")
    with model_path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    tokenizer_path = model_dir / "tokenizer.json"
    with tokenizer_path.open("rb") as stream:
        tokenizer_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    monkeypatch.setattr(runtime, "BUNDLED_EMBEDDING_SHA256", digest, raising=False)
    monkeypatch.setattr(runtime, "BUNDLED_TOKENIZER_SHA256", tokenizer_digest, raising=False)
    monkeypatch.setattr(runtime, "BUNDLED_EMBEDDING_DIM", 8, raising=False)

    app = create_app(settings)
    app.state.clock = lambda: NOW
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
        login = client.post(
            "/api/v1/auth/login",
            json={"role": "Admin", "email": SAMPLE_EMAIL, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        with database(settings) as db:
            demand_id = seed_demand(db, confirmed=False, jd="Required: SQL")
            candidate_id = seed_candidate(db)
            seed_resource(db, candidate=candidate_id, skills="SQL")

        analyzed = client.post(f"/api/v1/demands/{demand_id}/analyze", headers=headers)
        assert analyzed.status_code == 200, analyzed.text
        response = client.post(f"/api/v1/demands/{demand_id}/requirements/confirm", headers=headers)
        assert response.status_code == 202, response.text
        assert isinstance(response.json().get("runId"), int)
        run_id = finish(client, response)
        with database(settings) as db:
            run_status = db.execute(
                "SELECT status FROM match_runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            result_count = db.execute(
                "SELECT COUNT(*) FROM match_results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert run_status == "completed"
        assert result_count > 0


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


@pytest.mark.parametrize("mode", ["queued", "raise", "inactive", "empty"])
def test_confirm_incremental_trigger(
    api: Api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, mandatory=() if mode == "empty" else ("SQL",))
        seed_demand(db, "T1OTHER")
        old = {
            row[0]
            for row in db.execute(
                "SELECT i.item_id FROM jd_requirement_items i "
                "JOIN jd_requirements r USING(jd_req_id) "
                "JOIN demand_versions v USING(demand_version_id) WHERE v.demand_id=?",
                (demand_id,),
            )
        }
    draft = client.get(f"/api/v1/demands/{demand_id}/requirements").json()
    draft["mandatorySkills"] = [] if mode == "empty" else ["Rust", "Go"]
    response = client.put(f"/api/v1/demands/{demand_id}/requirements", headers=headers, json=draft)
    assert response.status_code == 200, response.text
    with database(client.app.state.settings) as db:
        new = {
            row[0]
            for row in db.execute(
                "SELECT i.item_id FROM jd_requirement_items i "
                "JOIN jd_requirements r USING(jd_req_id) "
                "JOIN demand_versions v USING(demand_version_id) "
                "WHERE v.demand_id=? AND r.status='draft'",
                (demand_id,),
            )
        }
    calls: list[set[tuple[OwnerType, int]]] = []

    def submit(
        runner: object,
        settings: Settings,
        owners: Iterable[tuple[OwnerType, int]],
        *,
        created_by: int | None = None,
    ) -> int | None:
        calls.append(set(owners))
        with database(settings) as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM jd_requirements WHERE status='superseded'"
                ).fetchone()[0]
                == 1
            )
        if mode == "raise":
            raise RuntimeError("Submission unavailable")
        return None if mode == "inactive" else 999

    monkeypatch.setattr("pfc.api.routers.match.submit_incremental_embed", submit)
    response = client.post(f"/api/v1/demands/{demand_id}/requirements/confirm", headers=headers)
    assert response.status_code == 202, response.text
    assert calls == ([{("jd_item", id) for id in old | new}] if old | new else [])
    assert client.get(f"/api/v1/demands/{demand_id}/requirements").json()["status"] == "confirmed"
    if mode in {"raise", "inactive"}:
        assert any(
            record.levelname == "WARNING" and "embedding" in record.message
            for record in caplog.records
        )


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
            "/match-runs/1/excluded",
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


def test_candidate_profile_parses_match_evidence_and_masks_email(api: Api) -> None:
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        seed_demand(db, mandatory=("SQL", "Python", "Rust"))
        candidate_id = seed_candidate(db)
        seed_resource(db, grade="C2", candidate=candidate_id, email=SAMPLE_EMAIL)
    run_id = finish(
        client, client.post("/api/v1/match-runs", headers=headers, json={"scope": "full"})
    )
    response = client.get(f"/api/v1/match-runs/{run_id}/results")
    assert response.status_code == 200, response.text
    row = response.json()[0]
    keys = ("matchingEvidence", "gapsChecks", "eligibilityEvidence")
    columns = ("matching_evidence", "gaps_checks", "eligibility_evidence")
    with database(settings) as db:
        stored = db.execute(
            "SELECT * FROM match_results WHERE result_id=?", (row["resultId"],)
        ).fetchone()
        for column in columns:
            assert isinstance(stored[column], str) and "; " in stored[column]
    profile = client.get(f"/api/v1/candidates/{candidate_id}")
    assert profile.status_code == 200, profile.text
    match = next(m for m in profile.json()["matches"] if m["resultId"] == row["resultId"])
    for key in keys:
        assert len(row[key]) >= 2
        assert match[key] == row[key]

    with database(settings) as db:
        db.execute(
            "UPDATE match_results SET matching_evidence=matching_evidence || ?,"
            "gaps_checks=gaps_checks || ?,eligibility_evidence=eligibility_evidence || ? "
            "WHERE result_id=?",
            (f"; Contact {SAMPLE_EMAIL}",) * 3 + (row["resultId"],),
        )
    masked = client.get(f"/api/v1/candidates/{candidate_id}")
    assert masked.status_code == 200, masked.text
    assert SAMPLE_EMAIL not in masked.text
    match = next(m for m in masked.json()["matches"] if m["resultId"] == row["resultId"])
    for key in keys:
        assert match[key] == [*row[key], "Contact Al********@capgemini.com"]


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
    assert draft["synonymsApplied"] == []
    assert draft["warnings"] == ["Rule-based fallback: provider_unavailable"]
    assert client.get(f"/api/v1/demands/{demand_id}/requirements").json() == draft
    # An existing confirmed revision survives any number of edits to the latest draft.
    with database(settings) as db:
        confirmed = confirm_requirements(db, demand_id, None, 1, NOW)["jd_req_id"]
    draft.update(
        experienceYears=5,
        experience=["5 years experience"],
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
    assert row["personEmailMasked"] == "Al********@capgemini.com" and SAMPLE_EMAIL not in rows.text
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
        expected_action = "exclude" if action == "include" else action
        assert (
            changed.status_code == 201 and changed.json()["override"]["action"] == expected_action
        )
        assert "newBand" not in changed.json()["override"]
    active_override = changed.json()["override"]["overrideId"]
    excluded = client.get(f"/api/v1/match-runs/{run_id}/excluded")
    assert excluded.status_code == 200, excluded.text
    global_rows = [item for item in excluded.json() if item["scope"] == "global"]
    pair_rows = [item for item in excluded.json() if item["scope"] == "pair"]
    assert len(global_rows) == 2 and all(item["shortName"] == "Excluded" for item in global_rows)
    assert all(item["demandId"] is None and item["codeRequisition"] is None for item in global_rows)
    assert len(pair_rows) == 1 and pair_rows[0]["shortName"] == "GradeGap"
    assert pair_rows[0]["reasonCode"] == "GRADE_GAP_GT1"
    assert pair_rows[0]["demandId"] == demand_id and pair_rows[0]["codeRequisition"] == "T1TEST"
    assert all(item["runId"] == run_id and item["detail"] for item in excluded.json())
    assert all(
        set(item)
        == {
            "runId",
            "resourceId",
            "shortName",
            "demandId",
            "codeRequisition",
            "reasonCode",
            "detail",
            "scope",
        }
        for item in excluded.json()
    )
    assert SAMPLE_EMAIL not in excluded.text
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
    assert saved.json()["experienceYears"] is None
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
        client.put(url, headers=headers, json={"demandId": demand_id, "jdReqId": 999}),
        409,
    )
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


def test_experience_metadata_and_targeted_year_edits(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        confirm_requirements(
            db,
            demand_id,
            {
                "experience": [
                    {"text": "Team leadership", "years": 2, "evidence": "Led teams"},
                    {"text": "Industry experience", "years": 5, "evidence": "Five years"},
                    {"text": "Mentoring", "evidence": "Mentored staff"},
                ]
            },
            1,
            NOW,
        )
        empty_id = seed_demand(db, "T1EMPTY", confirmed=False)
    url = f"/api/v1/demands/{demand_id}/requirements"
    original = client.get(url).json()

    def items(body: dict[str, Any]) -> list[dict[str, Any]]:
        with database(client.app.state.settings) as db:
            row = db.execute(
                "SELECT extraction_json FROM jd_requirements WHERE jd_req_id=?",
                (body["jdReqId"],),
            ).fetchone()
        return json.loads(row[0])["experience"]

    before = items(original)
    saved = client.put(url, headers=headers, json={**original, "preferredSkills": ["Python"]})
    assert saved.status_code == 200
    assert json.dumps(items(saved.json())).encode() == json.dumps(before).encode()
    omitted = {
        key: value
        for key, value in original.items()
        if key
        not in {
            "experience",
            "experienceYears",
        }
    }
    saved = client.put(url, headers=headers, json=omitted)
    assert saved.status_code == 200 and items(saved.json()) == before
    changed = client.put(url, headers=headers, json={**original, "experienceYears": 7})
    assert changed.status_code == 200
    expected = [dict(item) for item in before]
    expected[1]["years"] = 7
    assert items(changed.json()) == expected
    assert client.get(url).json()["experienceYears"] == 7
    lowered = client.put(url, headers=headers, json={**original, "experienceYears": 1})
    assert lowered.status_code == 200
    expected = [dict(item) for item in before]
    expected[0]["years"] = 1
    expected[1]["years"] = 1
    assert items(lowered.json()) == expected
    assert lowered.json()["experienceYears"] == 1
    assert client.get(url).json()["experienceYears"] == 1
    changed = client.put(
        url,
        headers=headers,
        json={
            **original,
            "experience": ["Team leadership", "New experience"],
        },
    )
    assert changed.status_code == 200
    assert items(changed.json()) == [
        before[0],
        {
            "text": "New experience",
            "evidence": None,
            "years": None,
        },
    ]
    cleared = client.put(url, headers=headers, json={**original, "experienceYears": None})
    assert cleared.status_code == 200 and all(
        item["years"] is None for item in items(cleared.json())
    )
    minimum = client.put(
        f"/api/v1/demands/{empty_id}/requirements",
        headers=headers,
        json={
            "demandId": empty_id,
            "experienceYears": 5,
            "experience": [],
        },
    )
    assert minimum.status_code == 200
    assert minimum.json()["experience"] == ["Minimum 5 years experience"]
    assert items(minimum.json())[0]["years"] == 5


def test_band_adjustment_remains_visible_after_pin(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_demand(db)
        seed_resource(db)
    run_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    rows_url = f"/api/v1/match-runs/{run_id}/results"
    row = client.get(rows_url).json()[0]
    assert row["activeOverrides"] == []
    endpoint = f"/api/v1/match-results/{row['resultId']}/overrides"
    adjusted = client.post(
        endpoint,
        headers=headers,
        json={
            "action": "adjust_band",
            "newBand": "Avg",
            "reason": "Reviewed band",
        },
    )
    assert adjusted.status_code == 201
    pinned = client.post(
        endpoint, headers=headers, json={"action": "pin", "reason": "Reviewed pin"}
    )
    assert pinned.status_code == 201
    for response in (
        pinned.json(),
        client.get(rows_url).json()[0],
        client.get(f"/api/v1/match-results/{row['resultId']}").json(),
    ):
        assert response["override"]["action"] == "adjust_band"
        assert response["override"]["newBand"] == "Avg"
        assert [value["action"] for value in response["activeOverrides"]] == ["adjust_band", "pin"]
    revoked = client.request(
        "DELETE",
        endpoint,
        headers=headers,
        params={
            "overrideId": adjusted.json()["override"]["overrideId"],
            "reason": "Undo band",
        },
    )
    assert revoked.status_code == 200 and revoked.json()["override"]["action"] == "pin"
    assert [value["action"] for value in revoked.json()["activeOverrides"]] == ["pin"]


def test_exclusion_takes_priority_over_band_adjustment(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_demand(db)
        seed_resource(db)
    run_id = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    row = client.get(f"/api/v1/match-runs/{run_id}/results").json()[0]
    endpoint = f"/api/v1/match-results/{row['resultId']}/overrides"
    adjusted = client.post(
        endpoint,
        headers=headers,
        json={
            "action": "adjust_band",
            "newBand": "Avg",
            "reason": "Review band",
        },
    )
    assert adjusted.status_code == 201
    excluded = client.post(
        endpoint, headers=headers, json={"action": "exclude", "reason": "Unsafe"}
    )
    assert excluded.status_code == 201 and excluded.json()["override"]["action"] == "exclude"
    assert [item["action"] for item in excluded.json()["activeOverrides"]] == [
        "adjust_band",
        "exclude",
    ]
    detail = client.get(f"/api/v1/match-results/{row['resultId']}")
    assert detail.status_code == 200 and detail.json()["override"]["action"] == "exclude"
    revoked = client.request("DELETE", endpoint, headers=headers, json={"reason": "Undo exclusion"})
    assert revoked.status_code == 200 and revoked.json()["override"]["action"] == "adjust_band"


def test_empty_missing_validation_production_openapi_and_removed_paths(api: Api) -> None:
    client, headers = api
    assert client.get("/api/v1/match-runs").json() == []
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
        "/match-runs/999/excluded",
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
    assert client.get(f"/api/v1/match-runs/{run_id}/excluded").json() == []
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
    assert "/api/v1/match-runs/{run_id}/excluded" in paths
    assert "/resources/excluded" not in {route.path for route in results_router.routes}
    assert "/api/v1/resources/excluded" in paths
    resource_routes = [
        route for route in resources_router.routes if route.path == "/resources/excluded"
    ]
    assert len(resource_routes) == 1
    assert resource_routes[0].endpoint.__module__ == "pfc.api.routers.resources"
    assert "/api/v1/match-results/{result_id}/overrides" in paths
    assert "/api/v1/demands/{demand_id}/matches" not in paths
    for path in (
        "/match/runs",
        "/results",
        "/results/excluded",
        f"/demands/{demand_id}/matches",
    ):
        problem(client.get("/api/v1" + path), 404)


LEGACY_MASKS = {
    SAMPLE_EMAIL: ("Al******er@capgemini.com", "Al********@capgemini.com"),
    "jonathan.smithers@example.com": (
        "jo******.smithers@example.com",
        "jo***************@example.com",
    ),
}


def test_req_out_03_runs_saved_under_the_old_mask_read_with_the_new_one(api: Api) -> None:
    """REQ-OUT-03: snapshots masked with the old 2+6 rule are re-masked on every read."""
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        seed_demand(db)
        candidate_id = seed_candidate(db)
        seed_resource(db, candidate=candidate_id, email=SAMPLE_EMAIL)
        seed_resource(db, "Jonathan", email="jonathan.smithers@example.com")
    run_id = finish(
        client, client.post("/api/v1/match-runs", headers=headers, json={"scope": "full"})
    )
    with database(settings) as db:
        for email, (legacy, _) in LEGACY_MASKS.items():
            db.execute(
                "UPDATE score_breakdowns SET inputs_json="
                "json_set(inputs_json,'$.display_facts.person_email',?) "
                "WHERE component='skills' AND result_id IN (SELECT result_id FROM match_results "
                "JOIN resources USING(resource_id) WHERE email_address=?)",
                (legacy, email),
            )
        legacy_text = "Contact " + " or ".join(legacy for legacy, _ in LEGACY_MASKS.values())
        db.execute(
            "UPDATE match_results SET matching_evidence=matching_evidence || ?",
            (f"; {legacy_text}",),
        )
    new = {masked for _, masked in LEGACY_MASKS.values()}
    new_text = "Contact " + " or ".join(masked for _, masked in LEGACY_MASKS.values())

    def no_leak(text: str) -> None:
        assert "******er@" not in text and "smithers" not in text
        assert SAMPLE_EMAIL not in text

    rows = client.get(f"/api/v1/match-runs/{run_id}/results")
    assert rows.status_code == 200, rows.text
    no_leak(rows.text)
    assert {row["personEmailMasked"] for row in rows.json()} == new
    assert all(row["matchingEvidence"][-1] == new_text for row in rows.json())
    for row in rows.json():
        detail = client.get(f"/api/v1/match-results/{row['resultId']}")
        assert detail.status_code == 200, detail.text
        no_leak(detail.text)
        skills = next(b for b in detail.json()["breakdown"] if b["component"] == "skills")
        assert skills["inputs"]["display_facts"]["person_email"] == row["personEmailMasked"]
    export = client.get(f"/api/v1/match-runs/{run_id}/export?format=csv")
    assert export.status_code == 200, export.text
    no_leak(export.text)
    assert all(masked in export.text for masked in new) and new_text in export.text
    profile = client.get(f"/api/v1/candidates/{candidate_id}")
    assert profile.status_code == 200, profile.text
    no_leak(profile.text)
    assert profile.json()["matches"][0]["matchingEvidence"][-1] == new_text
    checks = {c["id"]: c for c in client.get("/api/v1/analytics/match").json()["validation"]}
    assert checks["email-copy"]["passed"] and checks["masked-email"]["passed"], checks
    for unmasked in (SAMPLE_EMAIL, "AlexBordxr@capgemini.com"):
        with database(settings) as db:
            db.execute(
                "UPDATE score_breakdowns SET inputs_json="
                "json_set(inputs_json,'$.display_facts.person_email',?) "
                "WHERE component='skills' AND result_id IN (SELECT result_id FROM match_results "
                "JOIN resources USING(resource_id) WHERE email_address=?)",
                (unmasked, SAMPLE_EMAIL),
            )
        checks = {c["id"]: c for c in client.get("/api/v1/analytics/match").json()["validation"]}
        assert not checks["email-copy"]["passed"], (unmasked, checks)


def test_skill_suggestion_admin_routes_and_refresh(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate, skills="Kubernetes")
        db.execute(
            "UPDATE resources SET eligibility='excluded',exclusion_reason='NO_SKILLS' "
            "WHERE resource_id=?",
            (resource,),
        )

    suggestions = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    if suggestions.status_code == 403:
        assert suggestions.headers["content-type"].startswith("application/problem+json")
        return
    assert suggestions.status_code == 200, suggestions.text
    item = next(s for s in suggestions.json() if s["term"] == "Kubernetes")
    assert item["ct_people"] == 1 and item["cv_people"] == 0 and item["jd_demands"] == 0
    resource_row = next(
        r for r in client.get("/api/v1/resources").json()["items"] if r["resource_id"] == resource
    )
    assert resource_row["unrecognised_skills"] == ["Kubernetes"]
    profile = client.get(f"/api/v1/candidates/{candidate}")
    assert profile.status_code == 200, profile.text
    assert profile.json()["resource"]["unrecognisedSkills"] == ["Kubernetes"]
    with database(client.app.state.settings) as db:
        unchanged = db.execute(
            "SELECT eligibility,exclusion_reason FROM resources WHERE resource_id=?",
            (resource,),
        ).fetchone()
        assert tuple(unchanged) == ("excluded", "NO_SKILLS")
        assert db.execute("SELECT 1 FROM skills WHERE name='Kubernetes'").fetchone() is None

    accepted = client.post(
        "/api/v1/occ/skill-suggestions/accept",
        headers=headers,
        json={"items": [{"term": "Kubernetes"}]},
    )
    assert accepted.status_code == 200, accepted.text
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT eligibility,exclusion_reason FROM resources WHERE resource_id=?",
            (resource,),
        ).fetchone()
        assert tuple(row) == ("eligible", None)
        assert db.execute(
            "SELECT 1 FROM audit_log WHERE action='OCC_EDIT' "
            "AND entity_type='skill_synonyms' LIMIT 1"
        ).fetchone()


def test_skill_suggestion_accept_batch_rolls_back_on_invalid_item(api: Api) -> None:
    client, headers = api
    response = client.post(
        "/api/v1/occ/skill-suggestions/accept",
        headers=headers,
        json={"items": [{"term": "Kubernetes"}, {"term": "Kubernetes"}]},
    )
    if response.status_code == 403:
        return
    assert response.status_code == 422, response.text
    assert "Kubernetes" in response.json()["detail"]
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT 1 FROM skills WHERE name='Kubernetes'").fetchone() is None
        assert (
            db.execute("SELECT 1 FROM skill_synonyms WHERE term_norm='kubernetes'").fetchone()
            is None
        )


def test_skill_suggestion_unknown_canonical_is_404_and_rolls_back(api: Api) -> None:
    client, headers = api
    response = client.post(
        "/api/v1/occ/skill-suggestions/accept",
        headers=headers,
        json={
            "items": [
                {"term": "Kubernetes"},
                {"term": "K8s Platform", "canonical": "Missing canonical"},
            ]
        },
    )
    if response.status_code == 403:
        return
    assert response.status_code == 404, response.text
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT 1 FROM skills WHERE name='Kubernetes'").fetchone() is None


def test_skill_suggestion_maps_to_existing_canonical(api: Api) -> None:
    client, headers = api
    response = client.post(
        "/api/v1/occ/skill-suggestions/accept",
        headers=headers,
        json={
            "items": [
                {
                    "term": "K8s Platform",
                    "canonical": "Kubernetes",
                    "relation": "related",
                    "credit": 0.9,
                }
            ]
        },
    )
    if response.status_code == 403:
        return
    assert response.status_code == 404, response.text

    with database(client.app.state.settings) as db:
        db.execute("INSERT INTO skills(name,created_by) VALUES ('Kubernetes','admin')")
    response = client.post(
        "/api/v1/occ/skill-suggestions/accept",
        headers=headers,
        json={
            "items": [
                {
                    "term": "K8s Platform",
                    "canonical": "Kubernetes",
                    "relation": "related",
                    "credit": 0.9,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["canonical"] == "Kubernetes"
    assert response.json()[0]["relation"] == "related"
    assert response.json()[0]["credit"] == 0.9


def test_suggestions_use_latest_live_cv_and_include_selected_and_exclude_inactive(
    api: Api,
) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        candidate = seed_candidate(db)
        first = seed_resource(
            db,
            candidate=candidate,
            skills="Kubernetes, person@example.com, 07700 900123",
        )
        second = seed_resource(db, "Linked resource", candidate=candidate, skills="Kubernetes")
        seed_resource(
            db,
            "Off bench",
            skills="OffBenchTool",
            status="Selected",
            group="Not on Bench",
        )
        off_bench = seed_resource(db, "Off-bench lifecycle", skills="LifecycleOffBenchTool")
        db.execute("UPDATE resources SET lifecycle='off_bench' WHERE resource_id=?", (off_bench,))
        archived = seed_resource(db, "Archived", skills="ArchivedTool")
        db.execute("UPDATE resources SET lifecycle='archived' WHERE resource_id=?", (archived,))
        old = seed_cv(db, first, candidate, skills=("OldCVTool",))
        db.execute("UPDATE cv_versions SET is_latest=0 WHERE cv_version_id=?", (old,))
        latest = db.execute(
            "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
            "extracted_by,is_latest,extracted_at) SELECT resume_id,candidate_id,2,'cv-latest',"
            "'LatestCVTool','test',1,extracted_at FROM cv_versions WHERE cv_version_id=?",
            (old,),
        ).lastrowid
        db.execute(
            "INSERT INTO resource_skills(resource_id,cv_version_id,source,skill_raw,skill_norm,"
            "evidence) VALUES (?,?,'CV','LatestCVTool','latestcvtool','LatestCVTool')",
            (first, latest),
        )

    response = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    if response.status_code == 403:
        return
    assert response.status_code == 200, response.text
    suggestions = {item["term"]: item for item in response.json()}
    assert suggestions["Kubernetes"]["ct_people"] == 2
    assert suggestions["LatestCVTool"]["cv_people"] == 1
    assert "OldCVTool" not in suggestions
    assert suggestions["OffBenchTool"]["ct_people"] == 1
    assert "LifecycleOffBenchTool" not in suggestions and "ArchivedTool" not in suggestions
    assert "person@example.com" not in suggestions and "07700 900123" not in suggestions
    resources = client.get("/api/v1/resources").json()["items"]
    person = next(item for item in resources if item["resource_id"] == first)
    assert set(person["unrecognised_skills"]) == {"Kubernetes", "LatestCVTool"}
    assert "person@example.com" not in person["unrecognised_skills"]
    assert "07700 900123" not in person["unrecognised_skills"]

    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE cv_versions SET extraction_removed_at=? WHERE cv_version_id=?",
            (NOW, latest),
        )
    response = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    assert response.status_code == 200, response.text
    assert "LatestCVTool" not in {item["term"] for item in response.json()}

    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE resources SET lifecycle='archived' WHERE resource_id IN (?,?)",
            (first, second),
        )
    response = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    assert response.status_code == 200, response.text
    assert "Kubernetes" not in {item["term"] for item in response.json()}


def test_selected_resource_exposes_unrecognised_skills(api: Api) -> None:
    client, _ = api
    with database(client.app.state.settings) as db:
        selected = seed_resource(
            db,
            "Selected with unknown skills",
            skills="SelectedOnlyUnknownTool",
            status="Selected",
            group="Not on Bench",
        )

    response = client.get("/api/v1/resources")
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["items"] if row["resource_id"] == selected)
    assert item["unrecognised_skills"] == ["SelectedOnlyUnknownTool"]


def test_jd_suggestions_count_demands_and_filter_revision_kind_and_lifecycle(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        current = seed_demand(
            db,
            "T1JD01",
            mandatory=(
                "AI Governance",
                "SQL Server",
                "Kubernetes",
                "Strong Kubernetes experience",
                "Experience with: Kafka",
            ),
            preferred=("Stakeholder Management", "AI Governance"),
        )
        seed_demand(db, "T1JD02", mandatory=("AI Governance",))
        closed = seed_demand(db, "T1CLOSED", mandatory=("ClosedOnlySkill",))
        db.execute("UPDATE demands SET lifecycle='closed' WHERE demand_id=?", (closed,))
        version_id = db.execute(
            "SELECT current_version_id FROM demands WHERE demand_id=?", (current,)
        ).fetchone()[0]
        current_revision = db.execute(
            "SELECT jd_req_id FROM jd_requirements WHERE demand_version_id=? "
            "AND status='confirmed'",
            (version_id,),
        ).fetchone()[0]
        superseded = db.execute(
            "INSERT INTO jd_requirements(demand_version_id,revision,status,created_at) "
            "VALUES (?,2,'superseded',?)",
            (version_id, NOW),
        ).lastrowid
        db.execute(
            "INSERT INTO jd_requirement_items(jd_req_id,kind,text,position) "
            "VALUES (?, 'mandatory_skill','SupersededSkill',0)",
            (superseded,),
        )
        for position, kind, text in [
            (10, "experience", "Five years experience"),
            (11, "qualification", "Degree in Computer Science"),
            (12, "domain", "Insurance"),
        ]:
            db.execute(
                "INSERT INTO jd_requirement_items(jd_req_id,kind,text,position) VALUES (?,?,?,?)",
                (current_revision, kind, text, position),
            )

    response = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    if response.status_code == 403:
        return
    assert response.status_code == 200, response.text
    suggestions = {item["term"]: item for item in response.json()}
    assert suggestions["AI Governance"]["jd_demands"] == 2
    assert suggestions["SQL Server"]["jd_demands"] == 1
    assert suggestions["Kubernetes"]["jd_demands"] == 1
    assert suggestions["Stakeholder Management"]["jd_demands"] == 1
    assert "Strong Kubernetes experience" not in suggestions
    assert (
        not {
            "Experience with: Kafka",
            "ClosedOnlySkill",
            "SupersededSkill",
            "Five years experience",
            "Degree in Computer Science",
            "Insurance",
        }
        & suggestions.keys()
    )


def test_skill_suggestion_reject_restore_is_local_and_audited(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_resource(db, skills="Kubernetes")
    before = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    if before.status_code == 403:
        return
    assert before.status_code == 200, before.text
    rejected = client.post(
        "/api/v1/occ/skill-suggestions/reject",
        headers=headers,
        json={"terms": ["Kubernetes"]},
    )
    assert rejected.status_code == 200, rejected.text
    assert client.get("/api/v1/occ/skill-suggestions", headers=headers).json() == []
    assert rejected.json()[0]["term_norm"] == "kubernetes"
    restored = client.post(
        "/api/v1/occ/skill-suggestions/restore",
        headers=headers,
        json={"term_norms": ["kubernetes"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["updated"] == 1
    assert "Kubernetes" in {
        item["term"] for item in client.get("/api/v1/occ/skill-suggestions", headers=headers).json()
    }
    with database(client.app.state.settings) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM audit_log WHERE action='OCC_EDIT' "
                "AND entity_type='skill_term_rejections'"
            ).fetchone()[0]
            == 2
        )


def test_suggestions_group_raw_spellings_closest_and_any_existing_synonym(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_resource(db, "Typo A", skills="Projct Management")
        seed_resource(db, "Typo B", skills="projct management")
        canonical_id = db.execute(
            "SELECT skill_id FROM skills WHERE name='SQL' COLLATE NOCASE"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,credit,status,"
            "created_by,updated_at) VALUES (?,?,?,'equivalent',1,'rejected',1,?)",
            (canonical_id, "KnownAlias", "knownalias", NOW),
        )
        seed_resource(db, "Existing synonym", skills="KnownAlias")

    response = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    if response.status_code == 403:
        return
    assert response.status_code == 200, response.text
    suggestions = {item["term_norm"]: item for item in response.json()}
    typo = suggestions["projct management"]
    assert typo["term"] == "Projct Management"
    assert typo["ct_people"] == 2
    assert typo["closest"] == "Project Management"
    assert "knownalias" not in suggestions


def test_ct_suggestions_include_only_unrecognised_tokens(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_resource(
            db,
            skills=(
                "Kubernetes, Product Management, Generative AI, "
                "LineBreakSkillOne\nLineBreak Skill Two\r\nLineBreakSkillThree"
            ),
        )
    response = client.get("/api/v1/occ/skill-suggestions", headers=headers)
    if response.status_code == 403:
        return
    assert response.status_code == 200, response.text
    terms = {item["term"] for item in response.json()}
    assert "Kubernetes" in terms
    assert "Product Management" in terms
    assert "Generative AI" not in terms
    assert {"LineBreakSkillOne", "LineBreak Skill Two", "LineBreakSkillThree"} <= terms


def test_accept_does_not_rewrite_prior_match_run_results_or_exclusions(api: Api) -> None:
    client, headers = api
    if client.get("/api/v1/occ/skill-suggestions", headers=headers).status_code == 403:
        return
    with database(client.app.state.settings) as db:
        seed_demand(db, "T1SUGGEST", mandatory=("SQL",))
        unknown = seed_resource(db, "Unknown skill", skills="Kubernetes")
        seed_resource(db, "Recognised skill", skills="SQL")

    run_id = finish(
        client, client.post("/api/v1/match-runs", headers=headers, json={"scope": "full"})
    )
    with database(client.app.state.settings) as db:
        results_before = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM match_results WHERE run_id=? ORDER BY result_id", (run_id,)
            )
        ]
        exclusions_before = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM eligibility_exclusions WHERE run_id=? ORDER BY resource_id,"
                "demand_id,reason_code",
                (run_id,),
            )
        ]
        assert any(row[1] == unknown and row[3] == "NO_SKILLS" for row in exclusions_before)

    accepted = client.post(
        "/api/v1/occ/skill-suggestions/accept",
        headers=headers,
        json={"items": [{"term": "Kubernetes"}]},
    )
    assert accepted.status_code == 200, accepted.text
    with database(client.app.state.settings) as db:
        results_after = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM match_results WHERE run_id=? ORDER BY result_id", (run_id,)
            )
        ]
        exclusions_after = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM eligibility_exclusions WHERE run_id=? ORDER BY resource_id,"
                "demand_id,reason_code",
                (run_id,),
            )
        ]
    assert results_after == results_before
    assert exclusions_after == exclusions_before
