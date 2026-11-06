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
from pfc.services.matching.evidence import DURATION_GAP, apply_experience_checks


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


@pytest.mark.parametrize("cv", ["absent", "short", "unrelated", "grounded", "fabricated"])
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
                    "fabricated": "No duration recorded",
                }[cv]
            )
            facts = rule_facts(text).model_dump()
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
        assert (DURATION_GAP in row["gaps_checks"]) == (cv != "grounded")
        assert (DURATION_GAP in row["matching_evidence"]) == (cv != "grounded")
        if cv != "grounded":
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
        assert values[0][1].count(DURATION_GAP) == int(cv != "grounded")


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
