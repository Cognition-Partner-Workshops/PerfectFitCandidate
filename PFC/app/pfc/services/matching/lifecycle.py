"""Reconcile jobs that terminate before the service callback executes (REQ-MAT-08)."""

import sqlite3


def reconcile_runs(db: sqlite3.Connection, run_id: int | None = None) -> None:
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
