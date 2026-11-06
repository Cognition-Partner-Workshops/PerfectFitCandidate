import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.jobs.runner import TERMINAL, JobContext, JobRunner


@pytest.fixture
def runner(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, job_workers=1)
    migrate(settings)
    result = JobRunner(settings)
    yield result
    result.shutdown()


def wait_done(runner: JobRunner, job_id: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if runner.get(job_id).status in TERMINAL:
            return
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def test_complete_and_fail(runner: JobRunner) -> None:
    started, release = threading.Event(), threading.Event()

    def complete(context: JobContext) -> None:
        context.report_progress(0.4, "working")
        started.set()
        assert release.wait(5)
        with pytest.raises(ValueError):
            context.report_progress(1.1)

    job_id = runner.submit("example", complete, {"count": 1})
    assert started.wait(5)
    job = runner.get(job_id)
    assert (job.status, job.progress, job.message) == ("running", 0.4, "working")
    assert job.started_at
    release.set()
    wait_done(runner, job_id)
    job = runner.get(job_id)
    assert job.status == "completed" and job.progress == 1 and job.finished_at

    def fail(_: JobContext) -> None:
        raise RuntimeError("sensitive@example.com")

    failed = runner.submit("failure", fail)
    wait_done(runner, failed)
    assert runner.get(failed).status == "failed"
    assert runner.get(failed).message == "Job failed"
    assert runner.get(99999) is None
    assert runner.cancel(99999) is None


@pytest.mark.parametrize("metadata", ["snapshot", "missing", "none"])
def test_sqlite_failure_logs_error_code_without_payload(
    runner: JobRunner, caplog: pytest.LogCaptureFixture, metadata: str
) -> None:
    sentinel = "private-sqlite-error-payload"
    error = sqlite3.OperationalError(sentinel)
    if metadata == "snapshot":
        error.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
    elif metadata == "none":
        error.sqlite_errorname = None
        error.sqlite_errorcode = None

    def fail(_: JobContext) -> None:
        raise error

    with caplog.at_level(logging.ERROR, logger="pfc.jobs.runner"):
        job_id = runner.submit("sqlite_failure", fail)
        wait_done(runner, job_id)
    job = runner.get(job_id)
    assert job.status == "failed" and job.message == "Job failed"
    record = next(record for record in caplog.records if record.name == "pfc.jobs.runner")
    expected = "SQLITE_BUSY_SNAPSHOT, 517" if metadata == "snapshot" else "None, None"
    assert f"OperationalError ({expected})" in record.getMessage()
    assert record.exc_info is None
    assert sentinel not in caplog.text
    assert sentinel not in repr(record.args)


def test_cancel(runner: JobRunner) -> None:
    started = threading.Event()
    ran = threading.Event()

    def running(context: JobContext) -> None:
        started.set()
        assert context.cancelled.wait(5)
        assert context.is_cancelled()
        context.raise_if_cancelled()

    active = runner.submit("running", running)
    assert started.wait(5)
    queued = runner.submit("queued", lambda _: ran.set())
    assert runner.cancel(queued).status == "cancelled"
    assert not ran.is_set()
    runner.cancel(active)
    wait_done(runner, active)
    assert runner.get(active).status == "cancelled"
    runner.shutdown()
    assert not ran.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        runner.submit("closed", lambda _: None)


def test_restart(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    migrate(settings)
    with database(settings) as db:
        for status in ("queued", "running", "completed"):
            db.execute(
                "INSERT INTO jobs(kind,status,created_at) "
                "VALUES ('test',?, '2026-01-01T00:00:00Z')",
                (status,),
            )
    runner = JobRunner(settings)
    try:
        for job_id in (1, 2):
            job = runner.get(job_id)
            assert job.status == "failed"
            assert job.message == "interrupted by restart"
            assert job.finished_at
        assert runner.get(3).status == "completed"
    finally:
        runner.shutdown()


def test_events(runner: JobRunner) -> None:
    started, release = threading.Event(), threading.Event()

    def work(context: JobContext) -> None:
        context.report_progress(0.5, "halfway")
        started.set()
        assert release.wait(5)

    job_id = runner.submit("events", work)
    assert started.wait(5)

    async def collect() -> None:
        stream = runner.events(job_id, poll_interval=0.001, heartbeat=0)
        first = await anext(stream)
        assert first.startswith("event: progress\n")
        assert '"progress": 0.5' in first
        assert await anext(stream) == ": heartbeat\n\n"
        release.set()
        remaining = [event async for event in stream]
        assert remaining[-1].startswith("event: done\n")
        assert '"status": "completed"' in remaining[-1]
        assert [event async for event in runner.events(99999)] == []

    asyncio.run(collect())
