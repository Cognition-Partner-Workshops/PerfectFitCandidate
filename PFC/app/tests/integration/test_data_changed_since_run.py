"""Global change annotations preserve historical scoring and wire shapes."""

import csv
import io
import json
import sqlite3
from datetime import timedelta, timezone
from typing import Any

import pytest
from openpyxl import load_workbook
from test_analytics_export import populated as populated
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import NOW

from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.services.matching import get_run, list_runs

REASONS = ["DL/CT committed", "OCC approval edit", "JD confirmed", "CV synced"]
Populated = tuple[Api, dict[str, Any]]


def add_event(db: sqlite3.Connection, reason: str, at: str, *, action: str = "OCC_EDIT") -> None:
    if reason == "DL/CT committed":
        db.execute(
            "INSERT INTO ingestion_runs(kind,source,file_name,file_sha256,file_size,"
            "status,started_at,committed_at) "
            "VALUES ('CT','manual_upload','changed.xlsx',?,1,'committed',?,?)",
            (at, at, at),
        )
    elif reason == "OCC approval edit":
        append_audit(db, ts=at, action=action, user_id=1, entity_type="demand", entity_id="1")
    elif reason == "JD confirmed":
        db.execute(
            "UPDATE jd_requirements SET confirmed_at=? WHERE jd_req_id="
            "(SELECT MAX(jd_req_id) FROM jd_requirements)",
            (at,),
        )
    elif reason == "CV synced":
        db.execute(
            "INSERT INTO jobs(kind,status,created_at,finished_at) "
            "VALUES ('cv_sync','completed',?,?)",
            (at, at),
        )
    else:
        raise AssertionError(reason)


@pytest.mark.parametrize("reason", REASONS)
def test_each_change_is_global_and_visible_on_all_responses(
    populated: Populated, reason: str
) -> None:
    (client, _), ids = populated
    settings = client.app.state.settings
    with database(settings) as db:
        stored_runs = list(db.execute("SELECT * FROM match_runs"))
        stored_results = list(db.execute("SELECT * FROM match_results"))
        add_event(db, reason, (NOW + timedelta(seconds=1)).isoformat())
    url = f"/api/v1/match-runs/{ids['run']}"
    for response in (client.get(url), client.get("/api/v1/match-runs")):
        assert response.status_code == 200, response.text
        rows = response.json() if isinstance(response.json(), list) else [response.json()]
        assert all(row["dataChangedSinceRun"] for row in rows)
        assert all(row["dataChangedReasons"] == [reason] for row in rows)
    response = client.get(url + "/results")
    assert response.status_code == 200, response.text
    assert response.json()
    assert all(row["dataChangedSinceRun"] for row in response.json())
    assert all(row["dataChangedReasons"] == [reason] for row in response.json())
    empty = client.get(url + "/results", params={"requisition": "not-in-this-run"})
    assert empty.json() == []
    assert empty.headers["X-Data-Changed-Since-Run"] == "true"
    assert json.loads(empty.headers["X-Data-Changed-Reasons"]) == [reason]
    with database(settings) as db:
        assert list(db.execute("SELECT * FROM match_runs")) == stored_runs
        assert list(db.execute("SELECT * FROM match_results")) == stored_results


@pytest.mark.parametrize("action", ["OCC_EDIT", "OCC_APPROVE", "OCC_MODIFY", "OCC_REVERT"])
def test_approval_audit_actions(populated: Populated, action: str) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        add_event(db, "OCC approval edit", (NOW + timedelta(seconds=1)).isoformat(), action=action)
        run = get_run(db, ids["run"])
        assert run is not None and run["data_changed_reasons"] == ["OCC approval edit"]


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("delta", [-1, 0, 0.000001])
@pytest.mark.parametrize("precision", ["seconds", "microseconds"])
def test_event_boundaries_respect_stored_precision(
    populated: Populated, reason: str, delta: float, precision: str
) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        add_event(db, reason, (NOW + timedelta(seconds=delta)).isoformat(timespec=precision))
        run = get_run(db, ids["run"])
        assert run is not None
        expected = delta >= 0 if precision == "seconds" else delta > 0
        assert run["data_changed_since_run"] == expected
        assert run["data_changed_reasons"] == ([reason] if expected else [])


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("edit_microsecond", [100000, 900000])
def test_same_second_coarse_events_are_conservatively_flagged(
    populated: Populated, reason: str, edit_microsecond: int
) -> None:
    (client, _), ids = populated
    finished = NOW.replace(hour=10, microsecond=500000)
    edited = finished.replace(microsecond=edit_microsecond)
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE match_runs SET finished_at=? WHERE run_id=?", (finished.isoformat(), ids["run"])
        )
        add_event(db, reason, edited.isoformat(timespec="seconds").replace("+00:00", "Z"))
        run = get_run(db, ids["run"])
        assert run is not None and run["data_changed_since_run"]
        assert run["data_changed_reasons"] == [reason]


@pytest.mark.parametrize("reason", REASONS)
def test_coarse_event_is_not_hidden_by_later_precise_timestamp(
    populated: Populated, reason: str
) -> None:
    (client, _), ids = populated
    finished = NOW.replace(hour=10, microsecond=500000)
    coarse = finished.isoformat(timespec="seconds")
    precise = finished.replace(microsecond=400000).isoformat()
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE match_runs SET finished_at=? WHERE run_id=?", (finished.isoformat(), ids["run"])
        )
        add_event(db, reason, coarse)
        if reason == "JD confirmed":
            db.execute(
                "UPDATE jd_requirements SET confirmed_at=? WHERE jd_req_id="
                "(SELECT MIN(jd_req_id) FROM jd_requirements)",
                (coarse,),
            )
        add_event(db, reason, precise)
        run = get_run(db, ids["run"])
        assert run is not None and run["data_changed_reasons"] == [reason]


def test_new_run_is_unflagged_old_run_stays_flagged(populated: Populated) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        for reason in reversed(REASONS):
            add_event(db, reason, (NOW + timedelta(seconds=1)).isoformat())
            add_event(db, reason, (NOW + timedelta(seconds=2)).isoformat())
        later = (NOW + timedelta(seconds=3)).isoformat()
        new_id = db.execute(
            "INSERT INTO match_runs(scope,template_id,embedding_model_id,engine_version,"
            "synonyms_version,config_hash,status,started_at,finished_at) "
            "SELECT scope,template_id,embedding_model_id,engine_version,synonyms_version,"
            "config_hash,'completed',?,? FROM match_runs WHERE run_id=?",
            (later, later, ids["run"]),
        ).lastrowid
        queries: list[str] = []
        db.set_trace_callback(queries.append)
        runs = {row["run_id"]: row for row in list_runs(db)}
        db.set_trace_callback(None)
        assert runs[ids["run"]]["data_changed_reasons"] == REASONS
        assert runs[ids["run"]]["data_changed_since_run"]
        assert runs[new_id]["data_changed_reasons"] == []
        assert not runs[new_id]["data_changed_since_run"]
        assert len([query for query in queries if "julianday" in query]) == 4


@pytest.mark.parametrize("status", ["queued", "running"])
def test_unfinished_runs_are_unflagged(populated: Populated, status: str) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        add_event(db, "CV synced", (NOW + timedelta(days=1)).isoformat())
        db.execute(
            "UPDATE match_runs SET status=?,finished_at=NULL WHERE run_id=?", (status, ids["run"])
        )
        run = get_run(db, ids["run"])
        assert run is not None and not run["data_changed_since_run"]
        assert run["data_changed_reasons"] == []


def test_unrelated_events_and_incomplete_syncs_do_not_flag(populated: Populated) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        at = (NOW + timedelta(days=1)).isoformat()
        add_event(db, "OCC approval edit", at, action="export_results")
        for kind, status in (("cv_sync", "failed"), ("cv_sync", "running"), ("embed", "completed")):
            db.execute(
                "INSERT INTO jobs(kind,status,created_at,finished_at) VALUES (?,?,?,?)",
                (kind, status, at, at),
            )
        run = get_run(db, ids["run"])
        assert run is not None and not run["data_changed_since_run"]
        assert run["data_changed_reasons"] == []


@pytest.mark.parametrize("delta", [0, 1])
@pytest.mark.parametrize("precision", ["seconds", "microseconds"])
def test_event_offsets_compare_instants_not_timestamp_strings(
    populated: Populated, delta: int, precision: str
) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        at = (NOW + timedelta(seconds=delta)).astimezone(timezone(timedelta(hours=5, minutes=30)))
        add_event(db, "CV synced", at.isoformat(timespec=precision))
        run = get_run(db, ids["run"])
        expected = precision == "seconds" or delta > 0
        assert run is not None and run["data_changed_since_run"] == expected


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_export_header_contains_change_metadata(populated: Populated, format: str) -> None:
    (client, _), ids = populated
    with database(client.app.state.settings) as db:
        add_event(db, "CV synced", (NOW + timedelta(seconds=1)).isoformat())
    response = client.get(f"/api/v1/match-runs/{ids['run']}/export?format={format}")
    assert response.status_code == 200, response.text
    if format == "csv":
        header = next(csv.reader(io.StringIO(response.text)))[0]
    else:
        sheet = load_workbook(io.BytesIO(response.content)).active
        assert sheet is not None
        header = sheet.cell(1, 1).value
    assert "dataChangedSinceRun: true" in header
    assert 'dataChangedReasons: ["CV synced"]' in header


def test_missing_run_is_still_not_found(populated: Populated) -> None:
    (client, _), _ = populated
    for suffix in ("", "/results", "/export"):
        assert client.get("/api/v1/match-runs/999999" + suffix).status_code == 404
