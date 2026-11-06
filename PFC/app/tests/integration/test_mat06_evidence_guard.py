"""REQ-MAT-06: score-neutral warnings and durable, additive JD evidence flags."""

import json
from typing import Any

import pytest
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import (
    NOW,
    FakeLlm,
    seed_candidate,
    seed_cv,
    seed_demand,
    seed_resource,
)

from pfc.db.connection import database
from pfc.services.ingestion.cv_facts import rule_facts
from pfc.services.matching import MatchingPorts, confirm_requirements
from pfc.services.matching.evidence import (
    DURATION_GAP,
    apply_experience_checks,
    snapshot_experience,
)


def scores(db: Any, run: int) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT r.resource_id,r.score,b.component,b.points,b.max_points,b.inputs_json "
            "FROM match_results r JOIN score_breakdowns b ON b.result_id=r.result_id "
            "WHERE r.run_id=? ORDER BY r.resource_id,b.component",
            (run,),
        )
    ]


def test_experience_years_follow_edited_items_and_revision_ancestry(api: Api) -> None:
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        demand = seed_demand(db, confirmed=False)
        initial = confirm_requirements(
            db,
            demand,
            {
                "mandatory_skills": [{"text": "C#"}],
                "preferred_skills": [{"text": "Go"}],
                "experience": [
                    {"text": "SQL", "years": 10},
                    {"text": "Python", "years": 3},
                    {"text": "Java"},
                ],
            },
            1,
            NOW,
        )
    url = f"/api/v1/demands/{demand}/requirements"
    body = client.get(url).json()
    body["experience"] = ["Python", "Java", "SQL"]
    body["experienceYears"] = 12
    saved = client.put(url, headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    finish(client, client.post(url + "/confirm", headers=headers))

    def assert_mapping(expected: dict[str, float]) -> dict[str, Any]:
        with database(settings) as db:
            current = db.execute(
                "SELECT jd_req_id FROM jd_requirements WHERE status='confirmed'"
            ).fetchone()[0]
            snapshot = snapshot_experience(db, {"demand": {"jd_req_id": current}}, [])
            timed = snapshot["requirements"].get(str(current), [])
            assert {item["text"]: item["years"] for item in timed} == expected
            for item in timed:
                stored = db.execute(
                    "SELECT * FROM jd_requirement_items WHERE item_id=?", (item["item_id"],)
                ).fetchone()
                assert stored["jd_req_id"] == current
                assert stored["text"] == item["text"]
                assert stored["source_jd_req_id"] is not None
            return snapshot

    assert_mapping({"Python": 3, "SQL": 12})
    body = client.get(url).json()
    body["experience"] = ["Python", "Java"]
    saved = client.put(url, headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    finish(client, client.post(url + "/confirm", headers=headers))
    assert_mapping({"Python": 3})
    with database(settings) as db:
        roots = {item["text"]: item["item_id"] for item in initial["items"]}
        confirm_requirements(
            db,
            demand,
            {
                "experience": [
                    {"text": "Python", "years": 4, "jdReqId": roots["Python"]},
                    {"text": "SQL", "years": 15, "jdReqId": roots["SQL"]},
                ]
            },
            1,
            NOW,
        )
    assert_mapping({"Python": 4, "SQL": 15})
    body = client.get(url).json()
    body["experienceYears"] = None
    saved = client.put(url, headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    finish(client, client.post(url + "/confirm", headers=headers))
    assert assert_mapping({}) == {"requirements": {}, "facts": {}}


@pytest.mark.parametrize("timed", [False, True])
def test_experience_checks_touch_only_demands_with_years(
    api: Api, timed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_resource(db)
        seed_demand(db, code="PLAIN")
        for code, years in [("UNTIMED", None), ("TIMED", 10 if timed else None)]:
            demand = seed_demand(db, code=code, confirmed=False)
            confirm_requirements(
                db, demand, {"experience": [{"text": "SQL", "years": years}]}, 1, NOW
            )

    def check_updates(db: Any, run: int, snapshot: dict[str, Any]) -> None:
        before = {
            row["result_id"]: tuple(row)
            for row in db.execute("SELECT * FROM match_results WHERE run_id=?", (run,))
        }
        affected = {
            row["result_id"]
            for row in db.execute("SELECT * FROM match_results WHERE run_id=?", (run,))
            if snapshot["requirements"].get(str(row["jd_req_id"]))
        }
        assert len(affected) == int(timed)
        db.execute("CREATE TEMP TABLE experience_updates(result_id INTEGER)")
        db.execute(
            "CREATE TEMP TRIGGER experience_update AFTER UPDATE OF "
            "matching_evidence,gaps_checks ON match_results BEGIN "
            "INSERT INTO experience_updates VALUES (NEW.result_id); END"
        )
        untouched = before.keys() - affected
        for result_id in untouched:
            db.execute(
                "UPDATE score_breakdowns SET inputs_json='null' "
                "WHERE result_id=? AND component='skills'",
                (result_id,),
            )
        apply_experience_checks(db, run, snapshot)
        assert {
            row[0] for row in db.execute("SELECT result_id FROM experience_updates")
        } == affected
        for row in db.execute("SELECT * FROM match_results WHERE run_id=?", (run,)):
            if row["result_id"] in untouched:
                assert tuple(row) == before[row["result_id"]]
        if not timed:
            assert snapshot == {"requirements": {}, "facts": {}}

    monkeypatch.setattr("pfc.services.matching.runs.apply_experience_checks", check_updates)
    finish(client, client.post("/api/v1/match-runs", json={"scope": "full"}, headers=headers))


def test_experience_snapshot_rejects_misaligned_items(api: Api) -> None:
    client, _ = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        confirmed = confirm_requirements(
            db, demand, {"experience": [{"text": "SQL", "years": 10}]}, 1, NOW
        )
        db.execute(
            "UPDATE jd_requirement_items SET text='Python' WHERE jd_req_id=?",
            (confirmed["jd_req_id"],),
        )
        with pytest.raises(ValueError, match="does not match its confirmed extraction"):
            snapshot_experience(db, {"demand": {"jd_req_id": confirmed["jd_req_id"]}}, [])


@pytest.mark.parametrize("cv", ["absent", "short", "unrelated", "grounded", "quote_only", "fabricated"])
def test_sql_ten_years_keeps_score_and_adds_validation_gap(
    api: Api, cv: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        demand = seed_demand(db, confirmed=False)
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate)
        requirements = {
            "mandatory_skills": [{"text": "Python"}],
            "experience": [{"text": "SQL", "years": 10}],
        }
        confirmed = confirm_requirements(db, demand, requirements, 1, NOW)
        if cv != "absent":
            version = seed_cv(db, resource, candidate, skills=("SQL", "Python"))
            text = (
                "Experience\n"
                + {
                    "short": "2 years SQL",
                    "unrelated": "10 years Python",
                    "grounded": "10 years SQL",
                    "quote_only": "10 years SQL",
                    "fabricated": "No duration recorded",
                }[cv]
            )
            facts = rule_facts(text).model_dump()
            if cv == "quote_only":
                facts["experience"][0]["value"] = "SQL"
            if cv == "fabricated":
                facts["experience"] = [
                    {
                        "value": "10 years SQL",
                        "evidence": "10 years SQL",
                        "span": [0, 12],
                        "source": "llm",
                    }
                ]
            db.execute(
                "UPDATE cv_versions SET text_norm=?,facts_json=? WHERE cv_version_id=?",
                (text, json.dumps({"facts_schema": 1, "facts": facts}), version),
            )

    def check_neutrality(db: Any, run: int, snapshot: dict[str, Any]) -> None:
        before = scores(db, run)
        apply_experience_checks(db, run, snapshot)
        assert scores(db, run) == before

    monkeypatch.setattr("pfc.services.matching.runs.apply_experience_checks", check_neutrality)
    body = {"scope": "full", "asOfDate": NOW.date().isoformat()}
    run = finish(client, client.post("/api/v1/match-runs", json=body, headers=headers))
    with database(settings) as db:
        baseline = scores(db, run)
        row = db.execute("SELECT * FROM match_results WHERE run_id=?", (run,)).fetchone()
        assert (DURATION_GAP in row["gaps_checks"]) == (cv not in {"grounded", "quote_only"})
        assert (DURATION_GAP in row["matching_evidence"]) == (cv not in {"grounded", "quote_only"})
        if cv not in {"grounded", "quote_only"}:
            assert "SQL — SQL (CT)" not in row["matching_evidence"]
        # Reapply the presentation check to an explicitly unguarded row: all score bytes stay put.
        items = json.loads(
            db.execute(
                "SELECT inputs_json FROM score_breakdowns WHERE result_id=? AND component='skills'",
                (row["result_id"],),
            ).fetchone()[0]
        )["items"]
        db.execute(
            "UPDATE match_results SET matching_evidence=?,gaps_checks='' WHERE result_id=?",
            ("; ".join(item["evidence"] for item in items), row["result_id"]),
        )
        apply_experience_checks(
            db,
            run,
            {
                "requirements": {
                    str(confirmed["jd_req_id"]): [
                        {
                            "item_id": str(confirmed["items"][1]["item_id"]),
                            "text": "SQL",
                            "years": 10,
                        }
                    ]
                },
                "facts": {},
            },
        )
        assert scores(db, run) == baseline
        db.execute(
            "UPDATE match_results SET matching_evidence=?,gaps_checks=? WHERE result_id=?",
            (row["matching_evidence"], row["gaps_checks"], row["result_id"]),
        )
    incremental = finish(
        client,
        client.post(
            "/api/v1/match-runs",
            json={**body, "incremental": True},
            headers=headers,
        ),
    )
    full = finish(client, client.post("/api/v1/match-runs", json=body, headers=headers))
    with database(settings) as db:
        assert scores(db, full) == scores(db, incremental) == baseline
        values = [
            tuple(
                db.execute(
                    "SELECT matching_evidence,gaps_checks FROM match_results WHERE run_id=?",
                    (r,),
                ).fetchone()
            )
            for r in (incremental, full)
        ]
        assert values[0] == values[1]
        assert values[0][1].count(DURATION_GAP) == int(cv not in {"grounded", "quote_only"})


@pytest.mark.parametrize("quote", ["Required: SQL", "Not in JD", None])
@pytest.mark.parametrize("legacy", [False, True])
def test_jd_quote_flag_survives_review_and_confirmation(
    api: Api, quote: str | None, legacy: bool
) -> None:
    client, headers = api

    class Llm(FakeLlm):
        def extract_jd(self, text: str) -> dict[str, Any]:
            return {"mandatory_skills": [{"text": "SQL", "evidence": quote}]}

    client.app.state.matching_ports = MatchingPorts(client.app.state.matching_ports.embedder, Llm())
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        seed_resource(db)
    url = f"/api/v1/demands/{demand}"
    response = client.post(url + "/analyze", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    unverified = quote != "Required: SQL"
    assert body["mandatorySkills"] == ["SQL"]
    assert body["items"][0]["evidenceUnverified"] is unverified
    assert body["items"][0]["evidence"] == (None if unverified else quote)
    if legacy:
        body.pop("items")
    elif unverified:
        body["mandatorySkills"] = ["SQL advanced"]
        body["items"][0]["text"] = "SQL advanced"
        body["items"][0]["evidenceUnverified"] = False
    for _ in range(2):
        saved = client.put(url + "/requirements", headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result["items"][0]["evidenceUnverified"] is unverified
        assert result["items"][0]["evidence"] == (None if unverified else quote)
        body = result
        if legacy:
            body.pop("items")
    finish(client, client.post(url + "/requirements/confirm", headers=headers))
    final = client.get(url + "/requirements").json()
    assert final["status"] == "confirmed"
    assert final["items"][0]["evidenceUnverified"] is unverified
