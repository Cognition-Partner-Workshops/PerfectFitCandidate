"""Parallel extraction and private background-job diagnostics over real HTTP routes."""

import json
import logging
import sqlite3
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread, local
from typing import Any

import pytest
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import FakeEmbedder, FakeLlm, seed_demand, wait_done

from pfc.db.connection import connect, database
from pfc.jobs.runner import JobContext
from pfc.services import matching
from pfc.services.matching import MatchingPorts, requirements


@pytest.mark.parametrize("api", ["Admin"], indirect=True)
def test_analyze_waits_for_competing_writer_before_reading_revision(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = api
    settings = client.app.state.settings
    with database(settings) as db:
        demand_id = seed_demand(db, confirmed=False)
    ready, release, committed = Event(), Event(), Event()
    errors: list[BaseException] = []

    def writer() -> None:
        other = connect(settings)
        try:
            other.execute(
                "UPDATE demands SET updated_at='2026-11-07' WHERE demand_id=?", (demand_id,)
            )
            ready.set()
            assert release.wait(10)
            other.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            other.close()
            committed.set()

    thread = Thread(target=writer)
    current = requirements._current

    def track_connection(db: sqlite3.Connection, demand_id: int) -> sqlite3.Row:
        def trace(sql: str) -> None:
            if sql == "BEGIN IMMEDIATE" or sql.startswith("UPDATE jd_requirements SET status="):
                release.set()
                committed.wait(10)

        db.set_trace_callback(trace)
        return current(db, demand_id)

    class ContendedLlm(FakeLlm):
        def extract_jd(self, text: str) -> dict[str, Any]:
            thread.start()
            assert ready.wait(10)
            return super().extract_jd(text)

    monkeypatch.setattr(requirements, "_current", track_connection)
    client.app.state.matching_ports = MatchingPorts(FakeEmbedder(), ContendedLlm())
    analyze = matching.analyze_jd

    def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return analyze(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            print(f"{exc.sqlite_errorcode} {exc.sqlite_errorname}\n{traceback.format_exc()}")
            raise

    monkeypatch.setattr(matching, "analyze_jd", capture)
    try:
        response = client.post(f"/api/v1/demands/{demand_id}/analyze", headers=headers)
        assert response.status_code == 200, response.text
        assert not errors
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(10)


@pytest.mark.parametrize("api", ["Admin"], indirect=True)
def test_eight_parallel_analyze_requests_create_one_draft_each(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        ids = [seed_demand(db, f"T1{i:04}", confirmed=False) for i in range(8)]
    barrier = Barrier(8)
    state = local()
    current = requirements._current

    def track_connection(db: sqlite3.Connection, demand_id: int) -> sqlite3.Row:
        state.db = db
        return current(db, demand_id)

    class ParallelLlm(FakeLlm):
        def extract_jd(self, text: str) -> dict[str, Any]:
            assert not state.db.in_transaction
            barrier.wait(timeout=10)
            return super().extract_jd(text)

    monkeypatch.setattr(requirements, "_current", track_connection)
    client.app.state.matching_ports = MatchingPorts(FakeEmbedder(), ParallelLlm())
    analyze = matching.analyze_jd
    errors: list[str] = []

    def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return analyze(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            detail = f"{exc.sqlite_errorcode} {exc.sqlite_errorname}\n{traceback.format_exc()}"
            errors.append(detail)
            print(detail)
            raise

    monkeypatch.setattr(matching, "analyze_jd", capture)

    def request(demand_id: int) -> tuple[int, str]:
        response = client.post(f"/api/v1/demands/{demand_id}/analyze", headers=headers)
        return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(request, ids))
    assert [status for status, _ in responses] == [200] * 8, (responses, errors)
    assert all(json.loads(body)["extractionMethod"] == "llm" for _, body in responses)
    assert not errors
    with database(client.app.state.settings) as db:
        rows = db.execute(
            "SELECT v.demand_id,r.status,count(*) FROM jd_requirements r "
            "JOIN demand_versions v USING(demand_version_id) "
            "GROUP BY v.demand_id,r.status ORDER BY v.demand_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(id, "draft", 1) for id in sorted(ids)]


def test_failed_job_logs_traceback_but_returns_only_generic_message(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = api
    runner = client.app.state.job_runner

    def fail(_: JobContext) -> None:
        raise RuntimeError("private diagnostic marker")

    with caplog.at_level(logging.ERROR, logger="pfc.jobs.runner"):
        job_id = runner.submit("test_failure", fail)
        assert wait_done(runner, job_id).status == "failed"
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Job failed"
    assert "private diagnostic marker" not in response.text
    assert "Traceback" not in response.text
    with database(client.app.state.settings) as db:
        assert (
            db.execute("SELECT message FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            == "Job failed"
        )
    record = next(record for record in caplog.records if record.name == "pfc.jobs.runner")
    assert record.exc_info is not None
    assert "Traceback (most recent call last)" in caplog.text
    assert "RuntimeError: private diagnostic marker" in caplog.text
