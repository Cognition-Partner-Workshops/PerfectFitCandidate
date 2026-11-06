import pytest
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import seed_demand, seed_resource
from test_occ_domain_config import edit, result, run, snapshot, status_points, tiered, unchanged

from pfc.db.connection import database
from pfc.services.occ.domain import occ_domain_config

pytestmark = pytest.mark.parametrize("api", ["Admin"], indirect=True)


def test_reopened_status_inherits_priority_but_preserves_source_overrides(api: Api) -> None:
    tiered(api)
    with database(api[0].app.state.settings) as db:
        seed_demand(db)
        seed_resource(db, status="Selected")
    for source, excluded in (("Inherited", True), ("Overridden", False)):
        edit(
            api,
            f"status-map/{source}",
            {"canonical_code": "SELECTED", "priority": 4, "is_excluded": excluded},
        )
    first = run(api)
    before = snapshot(api, first)
    bench = next(
        row for row in api[0].get("/api/v1/occ/bench-statuses").json() if row["code"] == "SELECTED"
    )
    assert bench["is_excluded"] and bench["priority"] is None
    edit(api, "bench-statuses/SELECTED", {**bench, "priority": 5, "is_excluded": False})
    with database(api[0].app.state.settings) as db:
        config = occ_domain_config(db)
        assert config.bench_statuses.resolve("Selected").priority == 5
        assert config.bench_statuses.resolve("Inherited").priority == 5
        assert config.bench_statuses.resolve("Overridden").priority == 4
        assert not config.bench_statuses.resolve("Selected").is_excluded
    second = run(api)
    assert result(api, second)["status_priority"] == 5
    assert status_points(api, second) == 1
    unchanged(api, first, second, before)
