"""Exact extended chart values through authenticated HTTP and real SQLite."""

from datetime import timedelta
from typing import Any

import pytest
from test_analytics_export import populated as populated
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import NOW, seed_candidate, seed_demand

from pfc.api.schemas.analytics import DashboardFilters
from pfc.db.connection import database
from pfc.services.analytics.population import Population
from pfc.services.lifecycle import record_transition

URL = "/api/v1/analytics/extended"


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, True),
        ({"grade": ["b1", "C1"], "codeRequisition": ["r1"], "location": ["LEEDS"]}, True),
        ({"grade": ["C1"]}, False),
        ({"grade": [" b1 "]}, False),
        ({"scoreMin": 76}, False),
        ({"scoreMax": 74}, False),
        ({"incompleteIdentity": ["yes"]}, False),
        ({"mandatorySkills": ["gap"]}, False),
        ({"incompleteIdentity": ["no"], "mandatorySkills": ["met"], "scoreMin": 75}, True),
        ({"accountName": [""], "bu": []}, True),
    ],
)
def test_compiled_match_filter(api: Api, filters: dict[str, Any], expected: bool) -> None:
    row = {
        "requisition": "R1",
        "account": "Acme",
        "bu": "Platform",
        "role_location": "London",
        "demand_grade": "B1",
        "person_location": "Leeds",
        "status": "Bench",
        "effective_band": "Strong",
        "candidate_type": "Internal",
        "incomplete_identity": False,
        "mandatory_missing": 0,
        "score": 75,
    }
    with database(api[0].app.state.settings) as db:
        population = Population(db, DashboardFilters.model_validate(filters), NOW.date())
        assert population.match_filter(row) is expected
        assert population.match_filter(row) is expected


@pytest.mark.parametrize("filters", [{}, {"scoreMin": 61, "band": "Strong"}, {"grade": "B1"}])
def test_dashboard_snapshot_equals_individual_panels(
    extended: tuple[Api, dict[str, Any]], filters: dict[str, Any]
) -> None:
    client = extended[0][0]
    response = client.get("/api/v1/analytics/dashboard", params=filters)
    assert response.status_code == 200, response.text
    for panel, data in response.json().items():
        individual = client.get(f"/api/v1/analytics/{panel}", params=filters)
        assert individual.status_code == 200, individual.text
        assert data == individual.json()


def test_dashboard_snapshot_empty_auth_and_bad_run(api: Api) -> None:
    client = api[0]
    assert client.get("/api/v1/analytics/dashboard").json()["extended"]["upskilling"] == []
    assert client.get("/api/v1/analytics/dashboard", params={"runId": 999}).status_code == 404
    client.cookies.clear()
    assert client.get("/api/v1/analytics/dashboard").status_code == 401


def get(client: Any, **params: Any) -> dict[str, Any]:
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def extended(populated: tuple[Api, dict[str, Any]]) -> tuple[Api, dict[str, Any]]:
    api, ids = populated
    client, _ = api
    upload = NOW - timedelta(days=10)
    with database(client.app.state.settings) as db:
        candidate = seed_candidate(db)
        db.execute(
            "UPDATE candidates SET display_name='Contact person0@example.com',"
            "classification='Internal',identity_status='complete' WHERE candidate_id=?",
            (candidate,),
        )
        db.execute(
            "UPDATE resources SET candidate_id=? WHERE resource_id=?",
            (candidate, ids["resources"][0]),
        )
        db.execute(
            "UPDATE resources SET bench_status='Overall upskilling (New bench)',"
            "group_name='RG2',location='Manchester/Leeds/Leeds',skills_raw='SQL' "
            "WHERE resource_id=?",
            (ids["resources"][2],),
        )
        db.execute(
            "UPDATE resources SET exclusion_reason='status_excluded,not_on_bench' "
            "WHERE resource_id=?",
            (ids["resources"][3],),
        )
        ids["far"] = seed_demand(db, "T1FAR", grade="E1", mandatory=("Python",))
        ids["unknown"] = seed_demand(db, "T1UNKNOWN", mandatory=())
        db.execute("UPDATE demands SET code_local_grade=NULL WHERE demand_id=?", (ids["unknown"],))
        ingestion = db.execute("SELECT first_ingestion_id FROM demands LIMIT 1").fetchone()[0]
        db.execute("UPDATE demands SET first_ingestion_id=?", (ingestion,))
        db.execute("UPDATE ingestion_runs SET started_at=?", (upload.isoformat(),))
        db.execute("UPDATE match_results SET rank=6 WHERE result_id=?", (ids["results"][2],))
        db.execute(
            "UPDATE match_results SET mandatory_missing=1 WHERE result_id=?", (ids["results"][1],)
        )
        d2 = ids["demands"][2]
        version = db.execute(
            "SELECT current_version_id FROM demands WHERE demand_id=?", (d2,)
        ).fetchone()[0]
        req = db.execute(
            "SELECT jd_req_id FROM jd_requirements WHERE demand_version_id=?", (version,)
        ).fetchone()[0]
        ids["extra"] = db.execute(
            "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,resource_id,"
            "score,band,rank,shortlisted,grade_relation,location_assessment,matching_evidence,"
            "gaps_checks,eligibility_evidence) VALUES (?,?,?,?,?,75,'Potential',2,1,'exact',"
            "'Aligned','matched','checks','eligible')",
            (ids["run"], d2, version, req, ids["resources"][0]),
        ).lastrowid
        db.execute(
            "UPDATE match_results SET candidate_id=? WHERE resource_id=?",
            (candidate, ids["resources"][0]),
        )
        db.execute(
            "INSERT INTO overrides(demand_id,resource_id,result_id,action,reason,"
            "created_by,created_at) "
            "VALUES (?,?,?,'pin','reviewed',1,?)",
            (ids["demands"][0], ids["resources"][2], ids["results"][2], NOW.isoformat()),
        )
        messages = db.execute(
            "SELECT message_id FROM email_messages ORDER BY message_id"
        ).fetchall()
        for row, delta in zip(
            messages[:3],
            (timedelta(hours=12), timedelta(hours=36), timedelta(seconds=-1)),
            strict=True,
        ):
            db.execute(
                "UPDATE email_messages SET sent_at=? WHERE message_id=?",
                ((upload + delta).isoformat(), row[0]),
            )
    return api, ids


def archive_population(api: Api, ids: dict[str, Any]) -> None:
    with database(api[0].app.state.settings) as db:
        db.execute(
            "UPDATE candidates SET display_name='ArchivedOnlyName' WHERE candidate_id="
            "(SELECT candidate_id FROM resources WHERE resource_id=?)",
            (ids["resources"][0],),
        )
        db.execute(
            "UPDATE email_messages SET demand_id=?,sent_at=? "
            "WHERE message_id=(SELECT MAX(message_id) FROM email_messages)",
            (ids["demands"][2], NOW.isoformat()),
        )
        ids["archived_demands"] = [ids["demands"][0], ids["far"]]
        ids["archived_resources"] = [ids["resources"][i] for i in (0, 2, 3)]
        for entity in ("demand", "resource"):
            for entity_id in ids[f"archived_{entity}s"]:
                record_transition(
                    db,
                    entity,
                    entity_id,
                    "open" if entity == "demand" else "active",
                    "archived",
                    "archive",
                    now=NOW.isoformat(),
                    metadata={"archived_at": NOW.isoformat()},
                )


@pytest.fixture
def archived_extended(extended: tuple[Api, dict[str, Any]]) -> tuple[Api, dict[str, Any]]:
    api, ids = extended
    archive_population(api, ids)
    return api, ids


def test_archived_entities_absent_from_every_extended_dataset(
    archived_extended: tuple[Api, dict[str, Any]],
) -> None:
    api, ids = archived_extended
    data = get(api[0])
    assert len(data) == 15
    for cell in data["gradeMatrix"]:
        assert cell["demandCount"] == (2 if cell["row"] == "C1" else 0)
        assert cell["benchCount"] == (1 if cell["column"] == "C1" else 0)
        assert cell["count"] == cell["demandCount"] * cell["benchCount"]
    assert data["scoreHistogram"] == {
        "thresholds": [61, 70, 90],
        "bins": [
            {"lower": low, "upper": low + 5, "allPairs": int(low == 60), "top5": int(low == 60)}
            for low in range(0, 100, 5)
        ],
    }
    assert data["shortlistFunnel"] == [
        {"label": label, "count": count}
        for label, count in (
            ("Matched", 1),
            ("Reviewed / shortlisted", 0),
            ("Email approved", 0),
            ("Email sent", 0),
        )
    ]
    assert [(r["demandId"], r["reasonCodes"]) for r in data["unmatchedDemands"]] == [
        (ids["demands"][1], ["mandatory_skill_gap"]),
        (ids["demands"][2], ["other"]),
        (ids["unknown"], ["insufficient_data"]),
    ]
    assert data["emailTrend"] == []
    assert data["readinessStatus"] == [{"row": "RG1", "column": "Available", "count": 1}]
    assert data["locationMatrix"] == [{"row": "Manchester", "column": "Manchester", "count": 3}]
    assert data["skillCooccurrence"] == [
        {"row": row, "column": column, "count": 2}
        for row in ("Python", "SQL")
        for column in ("Python", "SQL")
    ]
    assert data["upskilling"] == []
    assert data["accuracy"] == [
        {
            "runId": ids["run"],
            "confirmedDemands": 0,
            "confirmedPairs": 0,
            "hits": 0,
            "hitRateAt5": None,
            "precisionAt5": None,
        }
    ]
    assert data["exclusionReasons"] == []
    assert data["candidateTypes"] == [
        {"label": label, "count": int(label == "Unknown")}
        for label in ("Internal", "External", "Unknown")
    ]
    assert data["contention"] == []
    assert data["demandAging"] == {
        "buckets": [
            {"label": label, "count": 3 if label == "7–13 days" else 0}
            for label in ("0–6 days", "7–13 days", "14–29 days", "30–59 days", "60+ days")
        ],
        "unknownStart": 0,
        "pastStartWithoutMatch": 0,
    }
    assert data["overrideRates"] == [
        {"runId": ids["run"], "pairCount": 1, "overriddenPairs": 0, "ratePct": 0, "byType": []}
    ]


@pytest.mark.parametrize("mode", ["current", "historical", "filtered"])
def test_archived_changes_cannot_affect_dashboard_snapshot(
    archived_extended: tuple[Api, dict[str, Any]], mode: str
) -> None:
    api, ids = archived_extended
    client = api[0]
    filters = (
        {"runId": ids["run"]}
        if mode == "historical"
        else {"scoreMin": 61}
        if mode == "filtered"
        else {}
    )

    def snapshot() -> dict[str, Any]:
        response = client.get("/api/v1/analytics/dashboard", params=filters)
        assert response.status_code == 200, response.text
        panels = response.json()
        for name, values in panels.items():
            individual = client.get(f"/api/v1/analytics/{name}", params=filters)
            assert individual.status_code == 200, individual.text
            assert individual.json() == values
        return panels

    before = snapshot()
    assert len(before["demand"]["rows"]) == 3
    assert [r["resourceId"] for r in before["bench"]["rows"]] == [ids["resources"][1]]
    assert before["summary"]["openRequisitions"] == 3
    assert before["summary"]["eligibleBench"] == 1
    assert before["summary"]["excludedBench"] == 0
    assert before["summary"]["roleToPersonCombinations"] == (0 if mode == "filtered" else 1)
    assert before["summary"]["run"]["pairsScored"] == 1
    assert before["summary"]["dataQualityIssues"] == {"total": 2, "dl": 1, "ct": 1}
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE demands SET code_local_grade='A3',region='ArchiveOnlyRegion',"
            "account_name='ArchiveOnlyAccount',position_start_date='2000-01-01' "
            "WHERE lifecycle='archived'"
        )
        db.execute(
            "UPDATE resources SET grade='A3',location='ArchiveOnlyLocation',"
            "group_name='ArchiveOnlyGroup',skills_raw='ArchiveOnlySkill',"
            "bench_status='Overall upskilling (New bench)',exclusion_reason='ArchiveOnlyReason',"
            "short_name='ArchiveOnlyPerson' WHERE lifecycle='archived'"
        )
        db.execute(
            "UPDATE match_results SET score=100,band='Strong',rank=1,mandatory_missing=0 "
            "WHERE demand_id IN (SELECT demand_id FROM demands WHERE lifecycle='archived') "
            "OR resource_id IN (SELECT resource_id FROM resources WHERE lifecycle='archived')"
        )
        db.execute("UPDATE overrides SET action='exclude'")
        db.execute("UPDATE email_messages SET status='Sent',sent_at=?", (NOW.isoformat(),))
    assert snapshot() == before


def test_grade_score_and_unmatched(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    data = get(api[0])
    ladder = ["A3", "A4", "B1", "B2", "C1", "C2", "D1", "D2", "E1"]
    assert [(c["row"], c["column"]) for c in data["gradeMatrix"]] == [
        (a, b) for a in ladder for b in ladder
    ]
    assert data["gradeMatrix"] == [
        {
            "row": a,
            "column": b,
            "count": (3 if a == "C1" else 1 if a == "E1" else 0) * (3 if b == "C1" else 0),
            "demandCount": 3 if a == "C1" else 1 if a == "E1" else 0,
            "benchCount": 3 if b == "C1" else 0,
            "relation": "exact" if i == j else "adjacent" if abs(i - j) == 1 else "outside",
        }
        for i, a in enumerate(ladder)
        for j, b in enumerate(ladder)
    ]
    assert data["scoreHistogram"] == {
        "thresholds": [61, 70, 90],
        "bins": [
            {
                "lower": low,
                "upper": low + 5,
                "allPairs": {60: 2, 75: 1, 90: 1}.get(low, 0),
                "top5": {60: 1, 75: 1, 90: 1}.get(low, 0),
            }
            for low in range(0, 100, 5)
        ],
    }
    assert data["unmatchedDemands"] == [
        {
            "demandId": ids["demands"][1],
            "codeRequisition": "T10001",
            "positionStartDate": "2026-11-16",
            "reasonCodes": ["mandatory_skill_gap"],
        },
        {
            "demandId": ids["far"],
            "codeRequisition": "T1FAR",
            "positionStartDate": "2026-11-16",
            "reasonCodes": ["no_person_in_grade_window"],
        },
        {
            "demandId": ids["unknown"],
            "codeRequisition": "T1UNKNOWN",
            "positionStartDate": "2026-11-16",
            "reasonCodes": ["insufficient_data"],
        },
    ]


def test_review_email_accuracy_and_overrides(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    data = get(api[0])
    assert data["shortlistFunnel"] == [
        {"label": "Matched", "count": 3},
        {"label": "Reviewed / shortlisted", "count": 1},
        {"label": "Email approved", "count": 1},
        {"label": "Email sent", "count": 1},
    ]
    assert data["emailTrend"] == [
        {"week": "2026-10-26", "sentCount": 2, "medianHours": 24.0, "within24hPct": 50.0}
    ]
    assert data["accuracy"] == [
        {
            "runId": ids["run"],
            "confirmedDemands": 1,
            "confirmedPairs": 2,
            "hits": 1,
            "hitRateAt5": 1.0,
            "precisionAt5": 1.0,
        }
    ]
    assert data["overrideRates"] == [
        {
            "runId": ids["run"],
            "pairCount": 4,
            "overriddenPairs": 1,
            "ratePct": 25.0,
            "byType": [{"label": "pin", "count": 1}],
        }
    ]


def test_supply_skills_people_and_aging(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    data = get(api[0])
    assert data["readinessStatus"] == [
        {"row": "RG1", "column": "Available", "count": 2},
        {"row": "RG1", "column": "Selected", "count": 1},
        {"row": "RG2", "column": "Overall upskilling (New bench)", "count": 1},
    ]
    assert data["locationMatrix"] == [
        {"row": "Manchester", "column": "Leeds", "count": 5},
        {"row": "Manchester", "column": "Manchester", "count": 15},
    ]
    assert data["skillCooccurrence"] == [
        {"row": "Python", "column": "Python", "count": 4},
        {"row": "Python", "column": "SQL", "count": 3},
        {"row": "SQL", "column": "Python", "count": 3},
        {"row": "SQL", "column": "SQL", "count": 3},
    ]
    assert data["upskilling"] == [
        {
            "resourceId": ids["resources"][2],
            "candidateId": 0,
            "name": "Person2",
            "emailMasked": "pe*****@example.com",
            "matchedSkills": ["SQL"],
            "missingSkills": ["Python"],
            "bestScore": 63.1,
        }
    ]
    assert data["exclusionReasons"] == [
        {"label": "not_on_bench", "count": 1},
        {"label": "status_excluded", "count": 1},
    ]
    assert data["candidateTypes"] == [
        {"label": "Internal", "count": 1},
        {"label": "External", "count": 0},
        {"label": "Unknown", "count": 3},
    ]
    candidate = data["contention"][0]
    assert candidate == {
        "resourceId": ids["resources"][0],
        "candidateId": candidate["candidateId"],
        "name": "Contact pe*****@example.com",
        "emailMasked": "pe*****@example.com",
        "demandCount": 2,
        "requisitions": ["T10000", "T10002"],
    }
    assert len(data["contention"]) == 1
    assert data["demandAging"] == {
        "buckets": [
            {"label": label, "count": count}
            for label, count in (
                ("0–6 days", 0),
                ("7–13 days", 5),
                ("14–29 days", 0),
                ("30–59 days", 0),
                ("60+ days", 0),
            )
        ],
        "unknownStart": 0,
        "pastStartWithoutMatch": 0,
    }
    assert "person0@example.com" not in str(data)
    assert data == get(api[0]) == get(api[0], runId=ids["run"])


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"band": "Strong"}, 1),
        ({"scoreMin": 70, "scoreMax": 90}, 1),
        ({"mandatorySkills": "gap"}, 1),
        ({"codeRequisition": "T10002"}, 1),
        ({"accountName": "absent"}, 0),
        ({"bu": "absent"}, 0),
        ({"region": "absent"}, 0),
        ({"grade": "E1"}, 0),
        ({"status": "Overall upskilling (New bench)"}, 1),
        ({"location": "Manchester/Leeds/Leeds"}, 1),
        ({"candidateType": "Internal"}, 2),
        ({"incompleteIdentity": "no"}, 4),
    ],
)
def test_filters_match_original_population(
    extended: tuple[Api, dict[str, Any]], filters: dict[str, Any], expected: int
) -> None:
    client = extended[0][0]
    data = get(client, **filters)
    assert sum(b["allPairs"] for b in data["scoreHistogram"]["bins"]) == expected
    original = client.get("/api/v1/analytics/match", params=filters)
    assert original.status_code == 200
    assert sum(c["count"] for c in original.json()["bandDistribution"]) == expected
    if set(filters) <= {"band", "scoreMin", "scoreMax", "mandatorySkills"}:
        assert data["emailTrend"] == get(client)["emailTrend"]
    bench = client.get("/api/v1/analytics/bench", params=filters)
    demand = client.get("/api/v1/analytics/demand", params=filters)
    assert bench.status_code == demand.status_code == 200
    assert sum(c["count"] for c in data["candidateTypes"]) == len(bench.json()["rows"])
    assert sum(c["count"] for c in data["readinessStatus"]) == len(bench.json()["rows"])
    assert sum(c["count"] for c in data["demandAging"]["buckets"]) == len(demand.json()["rows"])


def test_empty_auth_and_errors(api: Api) -> None:
    client, _ = api
    data = get(client)
    assert sum(c["count"] for c in data["gradeMatrix"]) == 0
    assert all(b["allPairs"] == b["top5"] == 0 for b in data["scoreHistogram"]["bins"])
    for key in (
        "unmatchedDemands",
        "emailTrend",
        "readinessStatus",
        "locationMatrix",
        "skillCooccurrence",
        "upskilling",
        "accuracy",
        "exclusionReasons",
        "contention",
        "overrideRates",
    ):
        assert data[key] == []
    assert all(s["count"] == 0 for s in data["shortlistFunnel"])
    assert client.get(URL, params={"runId": 99999}).status_code == 404
    for params in (
        {"runId": 0},
        {"scoreMin": 90, "scoreMax": 10},
        {"band": "bogus"},
        {"unexpected": "x"},
    ):
        assert client.get(URL, params=params).status_code == 422
    assert URL in client.app.openapi()["paths"]
    client.cookies.clear()
    assert client.get(URL).status_code == 401


def test_excluded_override_and_no_confirmation(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    client, _ = api
    with database(client.app.state.settings) as db:
        db.execute("DELETE FROM email_messages")
        db.execute("UPDATE overrides SET action='unpin'")
        db.execute("UPDATE match_results SET score=100 WHERE result_id=?", (ids["results"][0],))
    data = get(client)
    assert data["accuracy"] == [
        {
            "runId": ids["run"],
            "confirmedDemands": 0,
            "confirmedPairs": 0,
            "hits": 0,
            "hitRateAt5": None,
            "precisionAt5": None,
        }
    ]
    assert data["scoreHistogram"]["bins"][-1] == {
        "lower": 95,
        "upper": 100,
        "allPairs": 1,
        "top5": 1,
    }
    with database(client.app.state.settings) as db:
        db.execute("UPDATE overrides SET action='exclude'")
        db.execute(
            "UPDATE match_results SET band='Insufficient data' WHERE result_id=?",
            (ids["results"][1],),
        )
        db.execute(
            "UPDATE demands SET position_start_date='2020-01-01' WHERE demand_id=?",
            (ids["demands"][1],),
        )
    data = get(client)
    assert data["overrideRates"][0]["byType"] == [{"label": "exclude", "count": 1}]
    assert data["overrideRates"][0]["pairCount"] == 4
    assert sum(b["allPairs"] for b in data["scoreHistogram"]["bins"]) == 3
    assert data["unmatchedDemands"][0]["reasonCodes"] == [
        "mandatory_skill_gap",
        "insufficient_data",
    ]
    assert data["demandAging"]["pastStartWithoutMatch"] == 1
    with database(client.app.state.settings) as db:
        db.execute("UPDATE resources SET eligibility='excluded'")
        db.execute("UPDATE overrides SET revoked_at=?", (NOW.isoformat(),))
    data = get(client)
    assert data["unmatchedDemands"][0]["reasonCodes"][0] == "all_candidates_excluded"
    assert data["overrideRates"][0]["overriddenPairs"] == 0


def test_unmatched_other_and_invalid_dates(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    client, _ = api
    with database(client.app.state.settings) as db:
        db.execute("DELETE FROM match_results WHERE result_id=?", (ids["extra"],))
        db.execute("UPDATE ingestion_runs SET started_at='invalid'")
        db.execute("UPDATE demands SET created_at='invalid',position_start_date='invalid'")
    data = get(client)
    assert {r["demandId"]: r["reasonCodes"] for r in data["unmatchedDemands"]}[
        ids["demands"][2]
    ] == ["other"]
    assert data["demandAging"]["unknownStart"] == 5
    assert data["demandAging"]["pastStartWithoutMatch"] == 0
    assert data["emailTrend"] == []


def test_accuracy_nontrivial_ratios_and_revocation(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    client, _ = api
    with database(client.app.state.settings) as db:
        db.execute("UPDATE match_results SET rank=6 WHERE result_id=?", (ids["results"][1],))
        db.execute(
            "INSERT INTO overrides(demand_id,resource_id,result_id,action,reason,"
            "created_by,created_at) "
            "VALUES (?,?,?,'include','reviewed',1,?)",
            (ids["demands"][1], ids["resources"][1], ids["results"][1], NOW.isoformat()),
        )
        source = db.execute(
            "SELECT demand_version_id,jd_req_id FROM match_results WHERE result_id=?",
            (ids["results"][0],),
        ).fetchone()
        db.execute(
            "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,resource_id,"
            "score,band,rank,shortlisted,grade_relation,location_assessment,matching_evidence,"
            "gaps_checks,eligibility_evidence) VALUES (?,?,?,?,?,50,'Weak',2,1,'exact',"
            "'Aligned','matched','checks','eligible')",
            (ids["run"], ids["demands"][0], source[0], source[1], ids["resources"][1]),
        )
    accuracy = get(client)["accuracy"]
    assert accuracy == [
        {
            "runId": ids["run"],
            "confirmedDemands": 2,
            "confirmedPairs": 3,
            "hits": 1,
            "hitRateAt5": 0.5,
            "precisionAt5": 0.5,
        }
    ]


def test_current_per_demand_and_explicit_run(extended: tuple[Api, dict[str, Any]]) -> None:
    api, ids = extended
    client, _ = api
    with database(client.app.state.settings) as db:
        newer = db.execute(
            "INSERT INTO match_runs(scope,template_id,embedding_model_id,llm_model,engine_version,"
            "synonyms_version,config_hash,status,started_at,finished_at,demands_assessed,pairs_scored,"
            "roles_without_candidate,dq_summary_json) SELECT 'demands',template_id,"
            "embedding_model_id,"
            "llm_model,engine_version,synonyms_version,config_hash,status,started_at,finished_at,"
            "1,1,1,'{}' FROM match_runs WHERE run_id=?",
            (ids["run"],),
        ).lastrowid
        db.execute(
            "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,resource_id,"
            "candidate_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
            "matching_evidence,gaps_checks,eligibility_evidence) SELECT ?,demand_id,"
            "demand_version_id,jd_req_id,resource_id,candidate_id,35,'Weak',6,0,grade_relation,"
            "location_assessment,matching_evidence,gaps_checks,eligibility_evidence "
            "FROM match_results WHERE result_id=?",
            (newer, ids["extra"]),
        )
    current = get(client)
    historical = get(client, runId=ids["run"])
    explicit = get(client, runId=newer)
    assert sum(b["allPairs"] for b in current["scoreHistogram"]["bins"]) == 4
    assert current["scoreHistogram"]["bins"][7]["allPairs"] == 1
    assert historical["scoreHistogram"]["bins"][15]["allPairs"] == 1
    assert sum(b["allPairs"] for b in explicit["scoreHistogram"]["bins"]) == 1
    assert [r["runId"] for r in current["overrideRates"]] == [ids["run"], newer]
    assert current["contention"] == []
    assert current["unmatchedDemands"][0]["reasonCodes"] == ["mandatory_skill_gap"]
    with database(client.app.state.settings) as db:
        db.execute("UPDATE demands SET lifecycle='closed' WHERE demand_id=?", (ids["demands"][2],))
    assert sum(b["allPairs"] for b in get(client)["scoreHistogram"]["bins"]) == 3


def test_email_boundary_and_run_fallback(extended: tuple[Api, dict[str, Any]]) -> None:
    api, _ = extended
    client, _ = api
    with database(client.app.state.settings) as db:
        db.execute("UPDATE ingestion_runs SET started_at='invalid'")
        db.execute(
            "UPDATE email_messages SET sent_at=?,status='Sent'",
            ((NOW + timedelta(hours=24)).isoformat(),),
        )
    assert get(client)["emailTrend"] == [
        {"week": "2026-11-02", "sentCount": 4, "medianHours": 24.0, "within24hPct": 100.0}
    ]
