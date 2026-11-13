import csv
import io
from datetime import timedelta

from test_matching_api import Api, finish
from test_matching_service import NOW, seed_demand, seed_resource

from pfc.db.connection import database
from pfc.services.matching import confirm_requirements, save_draft

pytest_plugins = ("test_matching_api",)


def test_current_results_summary_excluded_export_filters_and_analytics(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demands = [
            seed_demand(db, code, mandatory=("SQL",)) for code in ("T10001", "T10004", "T10008")
        ]
        resources = [
            seed_resource(db, f"CurrentPerson{index}", skills="SQL, Python") for index in range(3)
        ]
        db.execute("UPDATE jd_requirements SET confirmed_at='2026-11-05T23:59:00+00:00'")

    runs = [
        finish(
            client,
            client.post(
                "/api/v1/match-runs",
                headers=headers,
                json={"scope": "demands", "demandIds": [demand_id]},
            ),
        )
        for demand_id in demands
    ]
    with database(client.app.state.settings) as db:
        for demand_id, base_score in zip(demands, (95.0, 85.0, 75.0), strict=True):
            rows = db.execute(
                "SELECT result_id,rank FROM match_results WHERE demand_id=? ORDER BY rank",
                (demand_id,),
            ).fetchall()
            for index, row in enumerate(rows):
                band = (
                    "Strong"
                    if demand_id == demands[0] and index == 0
                    else ("Potential" if demand_id == demands[1] and index < 2 else "Weak")
                )
                db.execute(
                    "UPDATE match_results SET band=?,score=? WHERE result_id=?",
                    (band, base_score - row["rank"], row["result_id"]),
                )
        version_id = db.execute(
            "SELECT current_version_id FROM demands WHERE demand_id=?", (demands[2],)
        ).fetchone()[0]
        db.execute(
            "UPDATE jd_requirements SET confirmed_at='2026-11-07T00:00:00+00:00' "
            "WHERE demand_version_id=?",
            (version_id,),
        )
        db.executemany(
            "INSERT INTO eligibility_exclusions"
            "(run_id,resource_id,demand_id,reason_code,detail) VALUES (?,?,?,?,?)",
            [
                (runs[0], resources[0], None, "NO_SKILLS", "global old"),
                (runs[2], resources[1], None, "NO_SKILLS", "global newest"),
                (runs[0], resources[0], demands[0], "GRADE_GAP_GT1", "pair one"),
                (runs[1], resources[1], demands[1], "GRADE_GAP_GT1", "pair two"),
                (runs[2], resources[2], demands[2], "GRADE_GAP_GT1", "pair three"),
            ],
        )

    current = client.get("/api/v1/match-results/current")
    assert current.status_code == 200, current.text
    assert current.headers["X-Total-Count"] == str(len(current.json()))
    assert {row["runId"] for row in current.json()} == set(runs)
    assert {row["codeRequisition"] for row in current.json()} == {"T10001", "T10004", "T10008"}
    assert any(
        row["codeRequisition"] == "T10008"
        and row["dataChangedSinceRun"]
        and "JD confirmed" in row["dataChangedReasons"]
        for row in current.json()
    )

    exact = client.get(
        "/api/v1/match-results/current",
        params=[("requisition", "T10001"), ("requisition", "T10008")],
    )
    assert exact.status_code == 200
    assert {row["codeRequisition"] for row in exact.json()} == {"T10001", "T10008"}
    assert client.get("/api/v1/match-results/current", params={"requisition": "T1000"}).json() == []

    bands = client.get(
        "/api/v1/match-results/current",
        params=[
            ("band", "Strong"),
            ("band", "Potential"),
            ("sort", "score"),
            ("descending", "true"),
        ],
    )
    assert bands.status_code == 200
    assert {row["band"] for row in bands.json()} == {"Strong", "Potential"}
    assert [row["score"] for row in bands.json()] == sorted(
        (row["score"] for row in bands.json()), reverse=True
    )
    per_run = client.get(
        f"/api/v1/match-runs/{runs[0]}/results",
        params=[("requisition", "T10001"), ("requisition", "T10008")],
    )
    assert per_run.status_code == 200
    assert {row["codeRequisition"] for row in per_run.json()} == {"T10001"}
    assert (
        client.get(
            f"/api/v1/match-runs/{runs[0]}/results", params={"requisition": "T10001"}
        ).status_code
        == 200
    )

    summary = client.get("/api/v1/match-results/current/summary")
    assert summary.status_code == 200, summary.text
    assert {item["runId"] for item in summary.json()["demands"]} == set(runs)
    assert {item["runId"] for item in summary.json()["runs"]} == set(runs)
    assert summary.json()["dataChangedSinceRun"]
    assert "JD confirmed" in summary.json()["dataChangedReasons"]
    assert demands[2] in summary.json()["staleDemandIds"]
    assert summary.json()["staleDemandIds"] == [demands[2]]

    excluded = client.get("/api/v1/match-results/current/excluded")
    assert excluded.status_code == 200, excluded.text
    assert {(item["scope"], item["runId"], item["demandId"]) for item in excluded.json()} == {
        ("global", runs[2], None),
        ("pair", runs[0], demands[0]),
        ("pair", runs[1], demands[1]),
        ("pair", runs[2], demands[2]),
    }
    filtered_excluded = client.get(
        "/api/v1/match-results/current/excluded", params={"requisition": "T10001"}
    )
    assert {(row["scope"], row["demandId"]) for row in filtered_excluded.json()} == {
        ("global", None),
        ("pair", demands[0]),
    }

    exported = client.get("/api/v1/match-results/current/export?format=csv")
    assert exported.status_code == 200, exported.text
    assert "PFC-results-current.csv" in exported.headers["content-disposition"]
    export_rows = list(csv.reader(io.StringIO(exported.text)))
    assert export_rows[0][0].startswith("Current results · Runs")
    assert {row[0] for row in export_rows[3:]} == {"T10001", "T10004", "T10008"}

    analytics = client.get("/api/v1/analytics/summary")
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()["requisitionsAssessed"] == 3
    assert analytics.json()["runsIncluded"] == 3
    assert analytics.json()["roleToPersonCombinations"] == len(current.json())
    current_filter = [
        ("requisition", "T10001"),
        ("requisition", "T10004"),
        ("band", "Strong"),
        ("band", "Potential"),
    ]
    filtered_rows = client.get("/api/v1/match-results/current", params=current_filter)
    assert filtered_rows.status_code == 200, filtered_rows.text
    assert len(filtered_rows.json()) == 3
    filtered_summary = client.get(
        "/api/v1/analytics/summary",
        params=[
            ("codeRequisition", "T10001"),
            ("codeRequisition", "T10004"),
            ("band", "Strong"),
            ("band", "Potential"),
        ],
    )
    assert filtered_summary.status_code == 200, filtered_summary.text
    assert filtered_summary.json()["requisitionsAssessed"] == 2
    assert filtered_summary.json()["roleToPersonCombinations"] == len(filtered_rows.json())
    explicit_filtered = client.get(
        "/api/v1/analytics/summary",
        params=[("runId", str(runs[0])), ("codeRequisition", "T10001"), ("band", "Strong")],
    )
    assert explicit_filtered.status_code == 200, explicit_filtered.text
    assert explicit_filtered.json()["roleToPersonCombinations"] == 1
    explicit = client.get(f"/api/v1/analytics/summary?runId={runs[0]}")
    assert explicit.status_code == 200
    assert explicit.json()["requisitionsAssessed"] == 1
    demand_rows = client.get(
        "/api/v1/analytics/demand",
        params=[("codeRequisition", "T10001"), ("codeRequisition", "T10008")],
    )
    assert {row["codeRequisition"] for row in demand_rows.json()["rows"]} == {
        "T10001",
        "T10008",
    }


def test_current_results_requires_analyst_role(api: Api) -> None:
    client, _ = api
    openapi = client.app.openapi()
    for path in (
        "/api/v1/match-results/current",
        "/api/v1/match-results/current/summary",
        "/api/v1/match-results/current/excluded",
        "/api/v1/match-results/current/export",
    ):
        assert path in openapi["paths"]
    assert "runsIncluded" in openapi["components"]["schemas"]["AnalyticsSummary"]["properties"]
    client.cookies.clear()
    assert client.get("/api/v1/match-results/current").status_code == 401


def test_result_rows_expose_effective_exclusion_with_later_include(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_id = seed_demand(db, "T10001", mandatory=("SQL",))
        seed_resource(db, "ExcludedCandidate", skills="SQL, Python")
    run_id = finish(
        client,
        client.post(
            "/api/v1/match-runs",
            headers=headers,
            json={"scope": "demands", "demandIds": [demand_id]},
        ),
    )
    run_path = f"/api/v1/match-runs/{run_id}/results"
    result = client.get(run_path).json()[0]
    result_id = result["resultId"]
    override_path = f"/api/v1/match-results/{result_id}/overrides"
    excluded = client.post(
        override_path, headers=headers, json={"action": "exclude", "reason": "Review"}
    )
    assert excluded.status_code == 201, excluded.text

    for path in (run_path, "/api/v1/match-results/current"):
        rows = client.get(path).json()
        assert next(row for row in rows if row["resultId"] == result_id)["effectiveExcluded"]

    included = client.post(
        override_path, headers=headers, json={"action": "include", "reason": "Restore"}
    )
    assert included.status_code == 201, included.text
    for path in (run_path, "/api/v1/match-results/current"):
        rows = client.get(path).json()
        assert not next(row for row in rows if row["resultId"] == result_id)["effectiveExcluded"]


def test_current_stale_demands_scope_jd_events_but_global_uploads_remain_global(
    api: Api,
) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand_a = seed_demand(db, "T20001", confirmed=False)
        demand_b = seed_demand(db, "T20002", confirmed=False)
        confirm_requirements(
            db,
            demand_a,
            {"mandatory_skills": [{"text": "SQL"}], "preferred_skills": []},
            1,
            NOW - timedelta(seconds=20),
        )
        seed_resource(db, "ScopedStaleCandidate", skills="SQL, Python")

    run_a = finish(
        client,
        client.post(
            "/api/v1/match-runs",
            headers=headers,
            json={"scope": "demands", "demandIds": [demand_a]},
        ),
    )
    with database(client.app.state.settings) as db:
        before_confirmation = (NOW - timedelta(seconds=10)).isoformat()
        db.execute(
            "UPDATE match_runs SET started_at=?,finished_at=? WHERE run_id=?",
            (before_confirmation, before_confirmation, run_a),
        )

    analyzed = client.post(f"/api/v1/demands/{demand_b}/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    confirmed = client.post(f"/api/v1/demands/{demand_b}/requirements/confirm", headers=headers)
    assert confirmed.status_code == 202, confirmed.text
    run_b = finish(client, confirmed)
    with database(client.app.state.settings) as db:
        after_confirmation = (NOW + timedelta(seconds=10)).isoformat()
        db.execute(
            "UPDATE match_runs SET finished_at=? WHERE run_id=?",
            (after_confirmation, run_b),
        )

    summary = client.get("/api/v1/match-results/current/summary")
    assert summary.status_code == 200, summary.text
    assert not summary.json()["dataChangedSinceRun"]
    assert summary.json()["dataChangedReasons"] == []
    assert summary.json()["staleDemandIds"] == []
    run_summaries = {run["runId"]: run for run in summary.json()["runs"]}
    assert run_summaries[run_a]["dataChangedSinceRun"]
    assert not run_summaries[run_b]["dataChangedSinceRun"]

    with database(client.app.state.settings) as db:
        save_draft(
            db,
            demand_a,
            {"mandatory_skills": [{"text": "SQL"}], "preferred_skills": []},
            1,
            NOW + timedelta(seconds=15),
        )

    draft_summary = client.get("/api/v1/match-results/current/summary")
    assert draft_summary.status_code == 200, draft_summary.text
    assert not draft_summary.json()["dataChangedSinceRun"]
    assert draft_summary.json()["staleDemandIds"] == []

    with database(client.app.state.settings) as db:
        confirm_requirements(db, demand_a, None, 1, NOW + timedelta(seconds=20))

    confirmed_summary = client.get("/api/v1/match-results/current/summary")
    assert confirmed_summary.status_code == 200, confirmed_summary.text
    assert confirmed_summary.json()["dataChangedSinceRun"]
    assert confirmed_summary.json()["staleDemandIds"] == [demand_a]
    assert confirmed_summary.json()["dataChangedReasons"] == ["JD confirmed"]

    with database(client.app.state.settings) as db:
        uploaded_at = (NOW + timedelta(seconds=30)).isoformat()
        db.execute(
            "INSERT INTO ingestion_runs(kind,source,file_name,file_sha256,file_size,"
            "status,started_at,committed_at) "
            "VALUES ('DL','manual_upload','later.xlsx','later',1,'committed',?,?)",
            (uploaded_at, uploaded_at),
        )

    uploaded_summary = client.get("/api/v1/match-results/current/summary")
    assert uploaded_summary.status_code == 200, uploaded_summary.text
    assert uploaded_summary.json()["staleDemandIds"] == sorted([demand_a, demand_b])
    assert uploaded_summary.json()["dataChangedReasons"] == ["DL/CT committed", "JD confirmed"]
