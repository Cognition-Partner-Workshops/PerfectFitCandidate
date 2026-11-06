"""Extraction provenance follows the reviewed revision lineage."""

import pytest
from test_jd_llm_extract import Provider
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import seed_demand, seed_resource

from pfc.db.connection import database
from pfc.services.matching import get_requirements


@pytest.mark.parametrize("mode", ["ready", "down"])
def test_edit_then_confirm_preserves_analysis_provenance(api: Api, mode: str) -> None:
    client, headers = api
    client.app.state.llm_provider = Provider(mode)
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        seed_resource(db)
    url = f"/api/v1/demands/{demand}"
    analyzed = client.post(url + "/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    original = analyzed.json()
    for skill in ("Python", "Go"):
        draft = client.get(url + "/requirements").json()
        draft["mandatorySkills"].append(skill)
        saved = client.put(url + "/requirements", headers=headers, json=draft)
        assert saved.status_code == 200, saved.text
    finish(client, client.post(url + "/requirements/confirm", headers=headers))
    confirmed = client.get(url + "/requirements").json()
    assert confirmed["status"] == "confirmed" and confirmed["extractedBy"] == "manual"
    for field in ("extractionMethod", "fallbackReason", "warnings"):
        assert confirmed[field] == original[field]

    client.app.state.llm_provider = Provider("down" if mode == "ready" else "ready")
    replacement = client.post(url + "/analyze", headers=headers).json()
    assert replacement["extractionMethod"] != original["extractionMethod"]
    saved = client.put(url + "/requirements", headers=headers, json=replacement)
    assert saved.status_code == 200, saved.text
    for field in ("extractionMethod", "fallbackReason", "warnings"):
        assert saved.json()[field] == replacement[field]
    with database(client.app.state.settings) as db:
        historical = get_requirements(db, demand, jd_req_id=confirmed["jdReqId"])
        assert historical is not None
        assert historical["extraction_method"] == original["extractionMethod"]
        assert historical["fallback_reason"] == original["fallbackReason"]


def test_manual_revision_does_not_inherit_future_analysis_or_another_jd_version(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}"
    body = {"demandId": demand, "mandatorySkills": ["SQL"]}
    manual = client.put(url + "/requirements", headers=headers, json=body)
    assert manual.status_code == 200, manual.text
    assert manual.json()["extractionMethod"] is None
    client.app.state.llm_provider = Provider("down")
    assert client.post(url + "/analyze", headers=headers).status_code == 200
    with database(client.app.state.settings) as db:
        historical = get_requirements(db, demand, jd_req_id=manual.json()["jdReqId"])
        assert historical is not None and historical["extraction_method"] is None
        db.execute(
            "INSERT INTO demand_versions(demand_id,version_no,jd_text_raw,jd_text_norm,"
            "jd_sha256,created_at) "
            "VALUES (?,2,'New JD','New JD','new','2026-11-06')",
            (demand,),
        )
        db.execute(
            "UPDATE demands SET current_version_id="
            "(SELECT MAX(demand_version_id) FROM demand_versions) "
            "WHERE demand_id=?",
            (demand,),
        )
    fresh = client.put(url + "/requirements", headers=headers, json=body)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["extractionMethod"] is None
    assert fresh.json()["fallbackReason"] is None and fresh.json()["warnings"] == []
