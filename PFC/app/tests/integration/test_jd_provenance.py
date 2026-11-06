"""Extraction provenance follows the reviewed revision lineage."""

import json

import pytest
from test_jd_llm_extract import Provider
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import NOW, FakeLlm, seed_demand, seed_resource

from pfc.adapters.llm.providers import FakeProvider
from pfc.db.connection import database
from pfc.services.matching import MatchingPorts, confirm_requirements, get_requirements


def test_classification_reason_survives_api_save_and_confirm(api: Api) -> None:
    client, headers = api
    client.app.state.llm_provider = FakeProvider(
        json.dumps(
            {
                "mandatory_skills": [{"text": "SQL"}, {"text": "PostgreSQL"}],
                "preferred_skills": [{"text": "PostgreSQL"}],
            }
        )
    )
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        seed_resource(db)
        db.execute(
            "UPDATE demand_versions SET jd_text_norm=? WHERE demand_version_id="
            "(SELECT current_version_id FROM demands WHERE demand_id=?)",
            ("Database specialist\nREQUIRED SKILLS: SQL\nPREFERRED SKILLS: PostgreSQL", demand),
        )
    url = f"/api/v1/demands/{demand}"
    response = client.post(url + "/analyze", headers=headers)
    assert response.status_code == 200, response.text
    original = response.json()
    assert original["mandatorySkills"] == ["SQL"]
    assert original["preferredSkills"] == ["PostgreSQL"]
    for _ in range(2):
        draft = client.get(url + "/requirements").json()
        kept = next(item for item in draft["items"] if item["text"] == "PostgreSQL")
        assert kept["classificationReason"] == "classification_adjusted"
        source_id = kept["jdReqId"]
        kept["classificationReason"] = None
        saved = client.put(url + "/requirements", headers=headers, json=draft)
        assert saved.status_code == 200, saved.text
        adjusted = next(item for item in saved.json()["items"] if item["text"] == "PostgreSQL")
        assert adjusted["classificationReason"] == "classification_adjusted"
        assert saved.json()["extractionMethod"] == "llm"
        assert saved.json()["fallbackReason"] is None
        with database(client.app.state.settings) as db:
            source = db.execute(
                "SELECT source_jd_req_id FROM jd_requirement_items WHERE item_id=?",
                (adjusted["jdReqId"],),
            ).fetchone()
            assert source[0] == source_id
            stored = db.execute(
                "SELECT extraction_json FROM jd_requirements WHERE jd_req_id=?",
                (saved.json()["jdReqId"],),
            ).fetchone()
            assert json.loads(stored[0])["preferred_skills"][0]["classification_reason"] == (
                "classification_adjusted"
            )
    finish(client, client.post(url + "/requirements/confirm", headers=headers))
    confirmed = client.get(url + "/requirements").json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["extractionMethod"] == original["extractionMethod"] == "llm"
    assert confirmed["fallbackReason"] is None and confirmed["warnings"] == []
    assert (
        next(item for item in confirmed["items"] if item["text"] == "PostgreSQL")[
            "classificationReason"
        ]
        == "classification_adjusted"
    )


@pytest.mark.parametrize("source_id", [None, 999999])
def test_manual_item_cannot_assert_classification_reason(api: Api, source_id: int | None) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    response = client.put(
        f"/api/v1/demands/{demand}/requirements",
        headers=headers,
        json={
            "demandId": demand,
            "mandatorySkills": ["SQL"],
            "items": [
                {
                    "kind": "mandatory_skill",
                    "text": "SQL",
                    "jdReqId": source_id,
                    "classificationReason": "classification_adjusted",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["classificationReason"] is None
    assert response.json()["extractionMethod"] is None
    assert response.json()["fallbackReason"] is None


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        ("preferred_skill", "MySQL", None),
        ("mandatory_skill", "PostgreSQL", None),
        ("preferred_skill", "PostgreSQL!", None),
        ("preferred_skill", "  ＰＯＳＴＧＲＥＳＱＬ  ", "classification_adjusted"),
    ],
)
def test_reused_item_id_requires_same_kind_and_normalized_text(
    api: Api, kind: str, text: str, expected: str | None
) -> None:
    client, headers = api
    client.app.state.llm_provider = FakeProvider(
        json.dumps(
            {
                "mandatory_skills": [{"text": "PostgreSQL"}],
                "preferred_skills": [{"text": "PostgreSQL"}],
            }
        )
    )
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        db.execute(
            "UPDATE demand_versions SET jd_text_norm=? WHERE demand_version_id="
            "(SELECT current_version_id FROM demands WHERE demand_id=?)",
            ("PREFERRED SKILLS: PostgreSQL", demand),
        )
    url = f"/api/v1/demands/{demand}"
    analyzed = client.post(url + "/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    source = analyzed.json()["items"][0]
    assert source["classificationReason"] == "classification_adjusted"
    response = client.put(
        url + "/requirements",
        headers=headers,
        json={
            "demandId": demand,
            "mandatorySkills" if kind == "mandatory_skill" else "preferredSkills": [text],
            "items": [{**source, "kind": kind, "text": text}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["classificationReason"] == expected
    assert client.get(url + "/requirements").json()["items"][0]["classificationReason"] == expected


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
    source_ids = {item["jdReqId"] for item in original["items"]}
    for skill in ("Python", "Go"):
        draft = client.get(url + "/requirements").json()
        draft["mandatorySkills"].append(skill)
        saved = client.put(url + "/requirements", headers=headers, json=draft)
        assert saved.status_code == 200, saved.text
        with database(client.app.state.settings) as db:
            retained = {
                row[0]
                for row in db.execute(
                    "SELECT source_jd_req_id FROM jd_requirement_items "
                    "WHERE jd_req_id=? AND source_jd_req_id IS NOT NULL",
                    (saved.json()["jdReqId"],),
                )
            }
            assert retained == source_ids
        source_ids = {item["jdReqId"] for item in saved.json()["items"]}
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


@pytest.mark.parametrize("mode", ["ready", "down"])
@pytest.mark.parametrize("keep_source", [False, True])
@pytest.mark.parametrize("skills", [[], ["Hand-typed independent requirement"]])
def test_independent_manual_draft_does_not_inherit_analysis_provenance(
    api: Api, mode: str, keep_source: bool, skills: list[str]
) -> None:
    client, headers = api
    client.app.state.llm_provider = Provider(mode)
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}"
    analyzed = client.post(url + "/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["extractionMethod"] is not None
    body = {"demandId": demand, "mandatorySkills": skills}
    if keep_source:
        body["jdReqId"] = analyzed.json()["jdReqId"]
    manual = client.put(
        url + "/requirements",
        headers=headers,
        json=body,
    )
    assert manual.status_code == 200, manual.text
    result = manual.json()
    assert result["extractionMethod"] is None
    assert result["fallbackReason"] is None
    assert result["warnings"] == []


def test_only_retained_analysis_items_continue_the_lineage(api: Api) -> None:
    client, headers = api
    client.app.state.llm_provider = Provider()
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}"
    draft = client.post(url + "/analyze", headers=headers).json()
    draft["mandatorySkills"].append("Python")
    draft = client.put(url + "/requirements", headers=headers, json=draft).json()
    assert draft["extractionMethod"] == "llm"
    for skills in (["Python"], ["SQL", "Python"]):
        draft["mandatorySkills"] = skills
        saved = client.put(url + "/requirements", headers=headers, json=draft)
        assert saved.status_code == 200, saved.text
        draft = saved.json()
        assert draft["extractionMethod"] is None
        assert draft["fallbackReason"] is None and draft["warnings"] == []


@pytest.mark.parametrize("source", ["unknown", "other_demand", "legacy"])
def test_untrusted_or_unrecorded_sources_do_not_establish_lineage(api: Api, source: str) -> None:
    client, headers = api
    client.app.state.llm_provider = Provider()
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        other = seed_demand(db, confirmed=False, code="OTHER")
    url = f"/api/v1/demands/{demand}"
    draft = client.post(url + "/analyze", headers=headers).json()
    if source == "unknown":
        draft["items"][0]["jdReqId"] = 999999
    elif source == "other_demand":
        foreign = client.post(f"/api/v1/demands/{other}/analyze", headers=headers).json()
        draft["items"] = foreign["items"]
    else:
        with database(client.app.state.settings) as db:
            legacy = confirm_requirements(
                db, demand, {"mandatory_skills": [{"text": "SQL"}]}, 1, NOW
            )
            assert legacy["extraction_method"] is None
            draft["jdReqId"] = legacy["jd_req_id"]
        draft["items"] = client.get(url + "/requirements").json()["items"]
    saved = client.put(url + "/requirements", headers=headers, json=draft)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["extractionMethod"] is None
    assert result["fallbackReason"] is None and result["warnings"] == []


def test_older_analysis_and_mixed_ancestry(api: Api) -> None:
    client, headers = api
    client.app.state.llm_provider = Provider()
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}"
    first = client.post(url + "/analyze", headers=headers).json()
    client.app.state.llm_provider = Provider("down")
    second = client.post(url + "/analyze", headers=headers).json()
    first["mandatorySkills"] = ["Edited older item"]
    first["items"][0]["text"] = "Edited older item"
    saved = client.put(url + "/requirements", headers=headers, json=first)
    assert saved.status_code == 200, saved.text
    assert saved.json()["extractionMethod"] == "llm"
    assert saved.json()["fallbackReason"] is None and saved.json()["warnings"] == []
    preferred = next(item for item in second["items"] if item["kind"] == "preferred_skill")
    mixed = saved.json()
    mixed["preferredSkills"] = [preferred["text"]]
    mixed["items"].append(preferred)
    saved = client.put(url + "/requirements", headers=headers, json=mixed)
    assert saved.status_code == 200, saved.text
    for field in ("extractionMethod", "fallbackReason", "warnings"):
        assert saved.json()[field] == second[field]


@pytest.mark.parametrize("field", ["qualifications", "domain"])
@pytest.mark.parametrize("texts", [[" Degree "], [" Degree ", "Degree", " Degree "]])
def test_trimmed_items_keep_distinct_sources(
    api: Api, monkeypatch: pytest.MonkeyPatch, field: str, texts: list[str]
) -> None:
    client, headers = api
    llm = FakeLlm()
    monkeypatch.setattr(llm, "extract_jd", lambda _: {field: [{"text": text} for text in texts]})
    client.app.state.matching_ports = MatchingPorts(llm=llm)
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}"
    analyzed = client.post(url + "/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    draft = analyzed.json()
    for _ in range(2):
        source_ids = [item["jdReqId"] for item in draft["items"]]
        saved = client.put(url + "/requirements", headers=headers, json=draft)
        assert saved.status_code == 200, saved.text
        draft = saved.json()
        assert draft["extractionMethod"] == "llm"
        assert draft["fallbackReason"] is None and draft["warnings"] == []
        with database(client.app.state.settings) as db:
            retained = [
                row[0]
                for row in db.execute(
                    "SELECT source_jd_req_id FROM jd_requirement_items WHERE jd_req_id=? "
                    "ORDER BY position,item_id",
                    (draft["jdReqId"],),
                )
            ]
        assert retained == source_ids


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
    analyzed = client.post(url + "/analyze", headers=headers)
    assert analyzed.status_code == 200
    body["items"] = analyzed.json()["items"]
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
