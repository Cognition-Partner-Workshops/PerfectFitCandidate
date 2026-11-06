"""Pre-guard bytes: match_pct is match_results.score; breakdown inputs stay opaque."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_analytics_equivalence import small as small
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import seed_candidate, seed_cv, seed_demand, seed_resource

from pfc.db.connection import database
from tests.perf.test_nfr01 import submit_and_wait

GOLDENS = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def matching(api: Api) -> tuple[Api, dict[str, Any]]:
    client, headers = api
    with database(client.app.state.settings) as db:
        seed_demand(db, mandatory=("SQL", "Python", "AWS"))
        seed_demand(db, "T1WEAK", mandatory=("SQL", "Rust", "Go", "Kotlin", "Cobol", "Scala", "R"))
        seed_demand(db, "T1DRAFT", confirmed=False)
        candidate = seed_candidate(db)
        resource = seed_resource(
            db, "CVPerson", skills="SQL", candidate=candidate, email="AlexBorder@capgemini.com"
        )
        seed_cv(db, resource, candidate)
        seed_resource(db, "SameCandidate", candidate=candidate)
        for i in range(6):
            seed_resource(db, f"Person{i}", skills="SQL, Python, AWS")
        seed_resource(
            db,
            "Global",
            status="Selected",
            group="Not on Bench",
            end="2026-11-16",
            end_status="valid",
        )
        seed_resource(db, "GradeGap", grade="E1")
        seed_resource(db, "Unparseable", end_status="unparseable")
        inactive = seed_resource(db, "Inactive")
        db.execute("UPDATE resources SET lifecycle='archived' WHERE resource_id=?", (inactive,))
    run_id, _ = submit_and_wait(client, {"scope": "full"}, headers["X-CSRF-Token"])
    return api, {"run": run_id}


def scoring_bytes(fixture: tuple[Api, dict[str, Any]]) -> bytes:
    (client, _), ids = fixture
    with database(client.app.state.settings) as db:
        scores = db.execute(
            "SELECT d.code_requisition,r.resource_id,r.score FROM match_results r "
            "JOIN demands d ON d.demand_id=r.demand_id WHERE r.run_id=? "
            "ORDER BY d.code_requisition,r.resource_id",
            (ids["run"],),
        ).fetchall()
        breakdowns = db.execute(
            "SELECT d.code_requisition,r.resource_id,b.component,b.points,b.max_points,"
            "b.inputs_json FROM score_breakdowns b JOIN match_results r "
            "ON r.result_id=b.result_id JOIN demands d ON d.demand_id=r.demand_id "
            "WHERE r.run_id=? ORDER BY d.code_requisition,r.resource_id,b.component",
            (ids["run"],),
        ).fetchall()
    return (
        json.dumps(
            {
                "match_pct": [list(r) for r in scores],
                "score_breakdown": [list(r) for r in breakdowns],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


@pytest.mark.parametrize("dataset", ["matching", "small"])
def test_req_mat_06_score_bytes(request: pytest.FixtureRequest, dataset: str, api: Api) -> None:
    actual = scoring_bytes(request.getfixturevalue(dataset))
    assert actual == (GOLDENS / f"mat06_scores_{dataset}.json").read_bytes()
