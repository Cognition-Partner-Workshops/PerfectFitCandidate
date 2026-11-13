import csv
import io

from test_matching_api import Api, finish
from test_matching_service import seed_demand, seed_resource

from pfc.db.connection import database

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
        for demand_id, band, base_score in zip(
            demands, ("Strong", "Potential", "Weak"), (95.0, 85.0, 75.0), strict=True
        ):
            rows = db.execute(
                "SELECT result_id,rank FROM match_results WHERE demand_id=? ORDER BY rank",
                (demand_id,),
            ).fetchall()
            for row in rows:
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
