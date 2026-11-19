"""Reconcile jobs that terminate before the service callback executes (REQ-MAT-08)."""

import sqlite3


def reconcile_runs(db: sqlite3.Connection, run_id: int | None = None) -> None:
    if (
        db.execute(
            "SELECT 1 FROM match_runs m JOIN jobs j "
            "ON j.job_id=json_extract(m.scope_json,'$.job_id') "
            "WHERE m.status IN ('queued','running') AND (? IS NULL OR m.run_id=?) "
            "AND j.status IN ('cancelled','failed') LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        is None
    ):
        return
    db.execute(
        "UPDATE match_runs SET status=(SELECT status FROM jobs "
        "WHERE job_id=json_extract(match_runs.scope_json,'$.job_id')),"
        "finished_at=(SELECT finished_at FROM jobs "
        "WHERE job_id=json_extract(match_runs.scope_json,'$.job_id')) "
        "WHERE status IN ('queued','running') AND (? IS NULL OR run_id=?) "
        "AND EXISTS (SELECT 1 FROM jobs WHERE "
        "job_id=json_extract(match_runs.scope_json,'$.job_id') "
        "AND jobs.status IN ('cancelled','failed'))",
        (run_id, run_id),
    )
