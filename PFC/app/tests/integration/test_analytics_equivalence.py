"""Exact pre-optimization wire responses, captured before changing analytics."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_analytics_export import populated as populated
from test_matching_api import Api
from test_matching_api import api as api

from pfc.db.connection import database
from pfc.services.matching import MatchingPorts
from tests.perf.test_nfr01 import FakeEmbedder, FakeVectorStore, seed_population, submit_and_wait

GOLDENS = Path(__file__).parents[1] / "fixtures" / "analytics"
CASES = {
    "none": {},
    "demand": {"accountName": "Account"},
    "person": {"location": "Manchester", "status": "Available"},
    "band": {"band": "Weak"},
    "score": {"scoreMin": 60, "scoreMax": 90},
    "mandatory_met": {"mandatorySkills": "met"},
    "mandatory_gap": {"mandatorySkills": "gap"},
    "explicit_run": {},
    "empty": {"codeRequisition": "absent"},
}


def capture(client: Any, run_id: int) -> dict[str, Any]:
    output = {}
    for case, filters in CASES.items():
        params = {"runId": run_id} if case == "explicit_run" else filters
        output[case] = {}
        for panel in ("summary", "demand", "bench", "match"):
            response = client.get(f"/api/v1/analytics/{panel}", params=params)
            assert response.status_code == 200, response.text
            output[case][panel] = response.json()
    return output


@pytest.fixture
def small(api: Api) -> tuple[Api, dict[str, Any]]:
    client, headers = api
    client.app.state.matching_ports = MatchingPorts(FakeEmbedder(), vector_store=FakeVectorStore())
    with database(client.app.state.settings) as db:
        seed_population(db, size=20)
    run_id, _ = submit_and_wait(client, {"scope": "full"}, headers["X-CSRF-Token"])
    with database(client.app.state.settings) as db:
        results = db.execute(
            "SELECT result_id FROM match_results WHERE run_id=? ORDER BY result_id LIMIT 3",
            (run_id,),
        ).fetchall()
    for row, body in zip(
        results,
        (
            {"action": "exclude", "reason": "Golden exclusion"},
            {"action": "adjust_band", "newBand": "Strong", "reason": "Golden review"},
            {"action": "pin", "reason": "Golden shortlist"},
        ),
        strict=True,
    ):
        response = client.post(
            f"/api/v1/match-results/{row[0]}/overrides", json=body, headers=headers
        )
        assert response.status_code == 201, response.text
    return api, {"run": run_id}


@pytest.mark.parametrize("dataset", ["populated", "small"])
def test_analytics_golden_equivalence(
    request: pytest.FixtureRequest, dataset: str, api: Api
) -> None:
    fixture, ids = request.getfixturevalue(dataset)
    actual = capture(fixture[0], ids["run"])
    expected = json.loads((GOLDENS / f"{dataset}.json").read_text())
    assert actual == expected
