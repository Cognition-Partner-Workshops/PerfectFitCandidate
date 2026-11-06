"""REQ-ELIG-03/07, REQ-MAT-05/08 and REQ-OUT-03 across real OCC routes."""

import json
import threading
from typing import Any

import pytest
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import NOW, seed_demand, seed_resource, wait_done

from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.jobs.runner import JobContext
from pfc.services.analytics import dashboard
from pfc.services.ingestion.repository import domain_config
from pfc.services.matching import confirm_requirements
from pfc.services.occ.domain import occ_domain_config
from pfc.services.pii import mask_email

pytestmark = pytest.mark.parametrize("api", ["Admin"], indirect=True)


def edit(api: Api, path: str, body: Any, method: str = "PUT") -> Any:
    client, headers = api
    response = client.request(method, "/api/v1/occ/" + path, headers=headers, json=body)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def run(api: Api) -> int:
    client, headers = api
    return finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))


def snapshot(api: Api, run_id: int) -> bytes:
    with database(api[0].app.state.settings) as db:
        rows = {
            "run": [
                dict(r) for r in db.execute("SELECT * FROM match_runs WHERE run_id=?", (run_id,))
            ],
            "results": [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM match_results WHERE run_id=? ORDER BY result_id", (run_id,)
                )
            ],
            "exclusions": [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM eligibility_exclusions WHERE run_id=? ORDER BY rowid",
                    (run_id,),
                )
            ],
            "breakdowns": [
                dict(r)
                for r in db.execute(
                    "SELECT b.* FROM score_breakdowns b JOIN match_results m USING(result_id) "
                    "WHERE m.run_id=? ORDER BY b.result_id,b.component",
                    (run_id,),
                )
            ],
        }
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()


def result(api: Api, run_id: int) -> dict[str, Any]:
    with database(api[0].app.state.settings) as db:
        return dict(db.execute("SELECT * FROM match_results WHERE run_id=?", (run_id,)).fetchone())


def status_points(api: Api, run_id: int) -> float:
    with database(api[0].app.state.settings) as db:
        return db.execute(
            "SELECT points FROM score_breakdowns JOIN match_results USING(result_id) "
            "WHERE run_id=? AND component='status'",
            (run_id,),
        ).fetchone()[0]


def unchanged(api: Api, first: int, second: int, before: bytes, *, synonym: bool = False) -> None:
    assert snapshot(api, first) == before
    with database(api[0].app.state.settings) as db:
        old, new = [
            db.execute("SELECT * FROM match_runs WHERE run_id=?", (id,)).fetchone()
            for id in (first, second)
        ]
        assert old["config_hash"] != new["config_hash"]
        assert (old["synonyms_version"] != new["synonyms_version"]) is synonym
        assert verify_chain(db)


def tiered(api: Api) -> None:
    draft = edit(api, "scoring-templates", {"change_note": "Tiered"}, "POST")
    rules = {**draft["rules"], "status_score_mode": "tiered"}
    edit(
        api, f"scoring-templates/{draft['template_id']}", {"rules": rules, "change_note": "Tiered"}
    )
    edit(api, f"scoring-templates/{draft['template_id']}/activate", {}, "POST")


def test_synonym_approval_changes_score_and_preserves_run(api: Api) -> None:
    alias = {
        "term": "pyalias",
        "canonical": "Python",
        "relation": "abbreviation",
        "credit": 1,
        "status": "suggested",
    }
    synonym = edit(api, "synonyms", alias, "POST")
    with database(api[0].app.state.settings) as db:
        seed_demand(db, mandatory=("Python",))
        seed_resource(db, skills="SQL, pyalias")
    first = run(api)
    before = snapshot(api, first)
    edit(api, f"synonyms/{synonym['synonym_id']}", {**alias, "status": "approved"})
    second = run(api)
    old, new = result(api, first), result(api, second)
    assert new["score"] > old["score"]
    assert old["mandatory_missing"] == 1 and new["mandatory_missing"] == 0
    assert "via synonym pyalias" in new["matching_evidence"]
    unchanged(api, first, second, before, synonym=True)


def test_status_priority_and_exclusion_change_new_runs_only(api: Api) -> None:
    tiered(api)
    with database(api[0].app.state.settings) as db:
        seed_demand(db)
        seed_resource(db)
    first = run(api)
    before = snapshot(api, first)
    bench = next(
        r for r in api[0].get("/api/v1/occ/bench-statuses").json() if r["code"] == "AVAILABLE"
    )
    edit(api, "bench-statuses/AVAILABLE", {**bench, "priority": 5})
    second = run(api)
    assert status_points(api, first) == 5 and status_points(api, second) == 1
    assert result(api, first)["score"] - result(api, second)["score"] == 4
    unchanged(api, first, second, before)
    second_before = snapshot(api, second)
    edit(api, "bench-statuses/AVAILABLE", {**bench, "priority": None, "is_excluded": True})
    third = run(api)
    with database(api[0].app.state.settings) as db:
        assert (
            db.execute("SELECT count(*) FROM match_results WHERE run_id=?", (third,)).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT reason_code FROM eligibility_exclusions WHERE run_id=?", (third,)
            ).fetchone()[0]
            == "STATUS_EXCLUDED"
        )
    unchanged(api, second, third, second_before)
    assert snapshot(api, first) == before


@pytest.mark.parametrize("excluded", [False, True])
def test_mapping_changes_unmapped_status_and_preserves_run(api: Api, excluded: bool) -> None:
    with database(api[0].app.state.settings) as db:
        seed_demand(db)
        seed_resource(db, status="New status")
    first = run(api)
    before = snapshot(api, first)
    assert "Unmapped status" in result(api, first)["gaps_checks"]
    assert status_points(api, first) == 0
    edit(
        api,
        "status-map/New status",
        {"canonical_code": "AVAILABLE", "priority": 1, "is_excluded": excluded},
    )
    second = run(api)
    if excluded:
        with database(api[0].app.state.settings) as db:
            assert (
                db.execute(
                    "SELECT reason_code FROM eligibility_exclusions WHERE run_id=?", (second,)
                ).fetchone()[0]
                == "STATUS_EXCLUDED"
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM match_results WHERE run_id=?", (second,)
                ).fetchone()[0]
                == 0
            )
    else:
        assert "Unmapped status" not in result(api, second)["gaps_checks"]
        assert status_points(api, second) == 5
    unchanged(api, first, second, before)


def test_grade_reordering_changes_pair_eligibility_and_preserves_run(api: Api) -> None:
    with database(api[0].app.state.settings) as db:
        seed_demand(db, grade="C1")
        seed_resource(db, grade="C2")
    first = run(api)
    before = snapshot(api, first)
    assert result(api, first)["grade_relation"] == "adjacent_above"
    codes = [r["code"] for r in api[0].get("/api/v1/occ/grades").json()]
    codes.remove("B2")
    codes.insert(codes.index("C1") + 1, "B2")
    edit(api, "grades", [{"code": code} for code in codes])
    second = run(api)
    with database(api[0].app.state.settings) as db:
        assert (
            db.execute("SELECT count(*) FROM match_results WHERE run_id=?", (second,)).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT reason_code FROM eligibility_exclusions WHERE run_id=?", (second,)
            ).fetchone()[0]
            == "GRADE_GAP_GT1"
        )
    unchanged(api, first, second, before)


def test_queued_run_freezes_occ_config_before_dispatch(api: Api) -> None:
    client, headers = api
    runner = client.app.state.job_runner
    started, release = threading.Event(), threading.Event()

    def block(context: JobContext) -> None:
        started.set()
        assert release.wait(10)

    blocker = runner.submit("blocker", block)
    assert started.wait(5)
    try:
        with database(client.app.state.settings) as db:
            seed_demand(db)
            seed_resource(db)
        response = client.post("/api/v1/match-runs", headers=headers, json={})
        assert response.status_code == 202, response.text
        bench = next(
            r for r in client.get("/api/v1/occ/bench-statuses").json() if r["code"] == "AVAILABLE"
        )
        edit(api, "bench-statuses/AVAILABLE", {**bench, "priority": None, "is_excluded": True})
        release.set()
        assert wait_done(runner, blocker).status == "completed"
        first = finish(client, response)
        assert result(api, first)["score"] > 0
        before = snapshot(api, first)
        second = run(api)
        with database(client.app.state.settings) as db:
            assert (
                db.execute(
                    "SELECT reason_code FROM eligibility_exclusions WHERE run_id=?", (second,)
                ).fetchone()[0]
                == "STATUS_EXCLUDED"
            )
        unchanged(api, first, second, before)
    finally:
        release.set()


def test_builder_uses_db_without_files_and_preserves_overrides(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    alias = {"term": "pyalias", "canonical": "Python", "relation": "abbreviation", "credit": 1}
    for term, status in (
        ("pyalias", "approved"),
        ("pyignored", "suggested"),
        ("pyrejected", "rejected"),
    ):
        edit(api, "synonyms", {**alias, "term": term, "status": status}, "POST")
    edit(
        api,
        "status-map/Custom",
        {"canonical_code": "AVAILABLE", "priority": 4, "is_excluded": True},
    )
    edit(
        api, "status-map/Unresolved", {"canonical_code": None, "priority": 2, "is_excluded": False}
    )
    bench = next(
        r for r in api[0].get("/api/v1/occ/bench-statuses").json() if r["code"] == "AVAILABLE"
    )
    edit(api, "bench-statuses/AVAILABLE", {**bench, "priority": 5})
    edit(
        api,
        "location-aliases",
        {"aliases": {" L.D.N ": " London "}, "travel_clusters": {"UK": [" LONDON ", "Reading"]}},
    )
    monkeypatch.setenv("PFC_CONFIG_DIR", str(tmp_path / "missing"))
    with database(api[0].app.state.settings) as db:
        config = occ_domain_config(db)
        assert domain_config(db).grades == config.grades
        assert config.synonyms.resolve("pyalias") == "Python"
        assert config.synonyms.resolve("pyignored") is None
        assert config.synonyms.resolve("pyrejected") is None
        assert {r.status for r in config.synonyms.rows} >= {"approved", "suggested", "rejected"}
        status = config.bench_statuses.resolve("Custom")
        assert status.priority == 4 and status.is_excluded
        assert config.bench_statuses.resolve("Unresolved") is None
        assert config.locations.aliases == {"l.d.n": "london"}
        assert config.locations.travel_clusters == {"uk": ("london", "reading")}
        assert verify_chain(db)


def test_empty_grades_remain_empty_without_overwriting_occ_edits(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    edit(
        api,
        "status-map/Custom",
        {"canonical_code": "AVAILABLE", "priority": 4, "is_excluded": False},
    )
    with database(api[0].app.state.settings) as db:
        db.execute("DELETE FROM grades")
        monkeypatch.setenv("PFC_CONFIG_DIR", str(tmp_path / "missing"))
        assert occ_domain_config(db).grades.codes == ()
        with pytest.raises(ValueError):
            occ_domain_config(db).grades.ordinal("C1")
        assert db.execute("SELECT count(*) FROM grades").fetchone()[0] == 0
        assert occ_domain_config(db).bench_statuses.resolve("Custom").priority == 4


@pytest.mark.parametrize("store", ["status_map", "bench_statuses", "skill_synonyms", "locations"])
def test_empty_occ_stores_remain_empty(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path, store: str
) -> None:
    with database(api[0].app.state.settings) as db:
        if store == "locations":
            db.execute("DELETE FROM settings WHERE key='location.aliases'")
        elif store == "bench_statuses":
            db.execute("DELETE FROM status_map")
            db.execute("DELETE FROM bench_statuses")
        else:
            db.execute(f"DELETE FROM {store}")
        changes = db.total_changes
        monkeypatch.setenv("PFC_CONFIG_DIR", str(tmp_path / "missing"))
        config = occ_domain_config(db)
        assert db.total_changes == changes
        if store in {"status_map", "bench_statuses"}:
            assert config.bench_statuses.resolve("Available") is None
        elif store == "skill_synonyms":
            assert config.synonyms.rows == ()
            assert config.synonyms.resolve("SQL") is None
        else:
            assert config.locations.aliases == config.locations.travel_clusters == {}


@pytest.mark.parametrize("failure", ["template", "locations"])
def test_invalid_reference_stores_fail_without_file_fallback(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path, failure: str
) -> None:
    with database(api[0].app.state.settings) as db:
        monkeypatch.setenv("PFC_CONFIG_DIR", str(tmp_path / "missing"))
        if failure == "template":
            db.execute("UPDATE scoring_templates SET status='retired'")
        else:
            db.execute(
                "UPDATE settings SET value_json=? WHERE key='location.aliases'",
                (json.dumps({"aliases": [], "travel_clusters": {}}),),
            )
        with pytest.raises(ValueError):
            occ_domain_config(db)


@pytest.mark.parametrize("action", ["analyze", "draft", "confirm", "confirm_edited"])
def test_jd_resolution_uses_occ_synonyms(api: Api, action: str) -> None:
    client, headers = api
    edit(
        api,
        "synonyms",
        {
            "term": "pyalias",
            "canonical": "Python",
            "relation": "abbreviation",
            "credit": 1,
            "status": "approved",
        },
        "POST",
    )
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False, jd="Required: pyalias")
    if action == "confirm_edited":
        with database(client.app.state.settings) as db:
            confirmed = confirm_requirements(
                db, demand, {"mandatory_skills": [{"text": "pyalias"}]}, 1, NOW
            )
            assert {r["canonical"] for r in confirmed["items"]} == {"Python"}
        return
    if action == "analyze":
        response = client.post(f"/api/v1/demands/{demand}/analyze", headers=headers)
    else:
        response = client.put(
            f"/api/v1/demands/{demand}/requirements",
            headers=headers,
            json={"demandId": demand, "mandatorySkills": ["pyalias"]},
        )
    assert response.status_code == 200, response.text
    if action == "confirm":
        finish(
            client, client.post(f"/api/v1/demands/{demand}/requirements/confirm", headers=headers)
        )
    with database(client.app.state.settings) as db:
        items = db.execute(
            "SELECT s.name FROM jd_requirement_items i JOIN skills s "
            "ON s.skill_id=i.canonical_skill_id JOIN jd_requirements q USING(jd_req_id) "
            "JOIN demand_versions v USING(demand_version_id) WHERE v.demand_id=?",
            (demand,),
        ).fetchall()
        assert items and {r[0] for r in items} == {"Python"}


@pytest.mark.parametrize(
    "email,expected",
    [
        ("AlexBorder@capgemini.com", "Al******er@capgemini.com"),
        ("a@x.com", "*@x.com"),
        ("ab@x.com", "**@x.com"),
        ("abc@x.com", "ab*@x.com"),
    ],
)
def test_resource_result_and_analytics_masks_are_identical(
    api: Api, email: str, expected: str
) -> None:
    with database(api[0].app.state.settings) as db:
        seed_demand(db)
        seed_resource(db, email=email)
        db.execute("UPDATE resources SET eligibility='eligible'")
    run_id = run(api)
    client = api[0]
    resource = client.get("/api/v1/resources").json()["items"][0]
    matched = client.get(f"/api/v1/match-runs/{run_id}/results").json()[0]
    analytics = client.get("/api/v1/analytics/bench").json()["rows"][0]
    assert (
        mask_email(email)
        == resource["email_address"]
        == matched["personEmailMasked"]
        == analytics["emailAddressMasked"]
        == expected
    )


def test_mask_email_empty_and_unformatted_values(api: Api) -> None:
    assert mask_email(None) is None
    assert mask_email("") == ""
    assert mask_email("a") == "*"
    assert mask_email("abcdefghijk") == "ab******ijk"


def test_validation_reuses_pure_checks_only_within_a_request(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    with database(api[0].app.state.settings) as db:
        seed_demand(db, "T10001")
        seed_demand(db, "T10002")
        seed_resource(db, email="AlexBorder@capgemini.com")
    run_id = run(api)
    calls: list[str] = []
    original = dashboard._mask_email

    def counted(value: str) -> str | None:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(dashboard, "_mask_email", counted)
    url = f"/api/v1/analytics/match?runId={run_id}"
    response = api[0].get(url)
    assert response.status_code == 200, response.text
    assert all(check["passed"] for check in response.json()["validation"])
    assert len(calls) == 1
    with database(api[0].app.state.settings) as db:
        db.execute("UPDATE resources SET email_address='changed@example.com'")
        db.execute(
            "UPDATE score_breakdowns SET inputs_json="
            "json_set(inputs_json,'$.display_facts.start_date','invalid') WHERE component='skills'"
        )
    checks = {item["id"]: item["passed"] for item in api[0].get(url).json()["validation"]}
    assert len(calls) == 2 and calls[-1] == "changed@example.com"
    assert not checks["email-copy"] and not checks["masked-email"] and not checks["start-date"]
