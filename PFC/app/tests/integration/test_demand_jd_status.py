"""Demand list and detail carry the JD status of the current demand version."""

import sqlite3

from fastapi.testclient import TestClient
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import seed_demand

from pfc.db.connection import database


def status(client: TestClient, demand: int) -> str:
    listed = client.get("/api/v1/demands?page_size=200")
    assert listed.status_code == 200, listed.text
    detail = client.get(f"/api/v1/demands/{demand}")
    assert detail.status_code == 200, detail.text
    by_id = {row["demand_id"]: row["jd_status"] for row in listed.json()["items"]}
    assert by_id[demand] == detail.json()["jd_status"]
    return str(detail.json()["jd_status"])


def new_jd_version(db: sqlite3.Connection, demand: int) -> None:
    version = db.execute(
        "INSERT INTO demand_versions"
        "(demand_id,version_no,jd_text_raw,jd_text_norm,jd_sha256,created_at)"
        " VALUES (?,2,'Required: Go','Required: Go','v2','2026-11-06T09:00:00Z')",
        (demand,),
    ).lastrowid
    db.execute("UPDATE demands SET current_version_id=? WHERE demand_id=?", (version, demand))


def test_not_analysed_without_requirements(api: Api) -> None:
    client, _ = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    assert status(client, demand) == "Not analysed"


def test_draft_after_analysis(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    analyzed = client.post(f"/api/v1/demands/{demand}/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    assert status(client, demand) == "Draft"


def test_confirmed_for_current_version(api: Api) -> None:
    client, _ = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db)
    assert status(client, demand) == "Confirmed"


def test_draft_edit_of_confirmed_requirements_is_draft(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db)
    url = f"/api/v1/demands/{demand}/requirements"
    draft = client.get(url).json()
    draft["mandatorySkills"].append("Python")
    saved = client.put(url, headers=headers, json=draft)
    assert saved.status_code == 200, saved.text
    assert status(client, demand) == "Draft"


def test_jd_changed_until_the_new_version_is_analysed(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db)
        new_jd_version(db, demand)
    assert status(client, demand) == "JD changed"
    analyzed = client.post(f"/api/v1/demands/{demand}/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    assert status(client, demand) == "Draft"


def test_one_list_page_keeps_each_demand_status(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        fresh = seed_demand(db, "T1NEW", confirmed=False)
        drafted = seed_demand(db, "T1DRAFT", confirmed=False)
        confirmed = seed_demand(db, "T1DONE")
        changed = seed_demand(db, "T1CHANGED")
        new_jd_version(db, changed)
    assert client.post(f"/api/v1/demands/{drafted}/analyze", headers=headers).status_code == 200
    rows = client.get("/api/v1/demands?page_size=200").json()["items"]
    assert {row["demand_id"]: row["jd_status"] for row in rows} == {
        fresh: "Not analysed",
        drafted: "Draft",
        confirmed: "Confirmed",
        changed: "JD changed",
    }
